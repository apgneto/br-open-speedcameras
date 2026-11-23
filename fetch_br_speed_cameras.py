#!/usr/bin/env python3
"""
Layer 1 (Static Daily Open-Data Pipeline) — Brazil Fixed Speed & Red-Light Cameras.

Queries OpenStreetMap Overpass tiled by Brazilian macro-regions (avoiding Overpass truncation/timeouts),
extracting:
  - `highway=speed_camera` nodes
  - `enforcement` nodes & relations (`maxspeed`, `traffic_signals`, `average_speed`)
  - Exact heading from: (1) explicit `direction` tag, (2) `enforcement` relation `from`->`to` nodes,
    or (3) parent `highway` way adjacent vertices
  - `road_name`, `bidirectional`, `fov_deg` (15 deg expressways / 35 deg urban), `alert_distance_m`

Optionally merges & deduplicates official public CSVs (DNIT / ANTT / DER) via `--merge-csv`.
License of OSM data: OpenStreetMap ODbL 1.0 (https://www.openstreetmap.org/copyright).
"""

import argparse
import csv
import datetime as dt
import json
import math
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# Regional tiles covering all of Brazil without triggering Overpass memory/output limits
BRAZIL_TILES = [
    ("Sul", "-33.8,-57.7,-22.5,-48.0"),
    ("SP_RJ_ES", "-25.4,-53.2,-17.8,-39.6"),
    ("MG_BA_SE", "-23.0,-51.1,-8.5,-37.0"),
    ("CentroOeste", "-24.1,-61.7,-7.3,-45.8"),
    ("Nordeste", "-12.5,-48.8,-1.0,-34.7"),
    ("Norte", "-13.7,-74.0,5.3,-46.0"),
]

CARDINAL_DEG = {
    "N": 0, "NNE": 22, "NE": 45, "ENE": 67,
    "E": 90, "ESE": 112, "SE": 135, "SSE": 157,
    "S": 180, "SSW": 202, "SW": 225, "WSW": 247,
    "W": 270, "WNW": 292, "NW": 315, "NNW": 337,
}


def parse_speed(raw: str | None) -> int | None:
    if not raw:
        return None
    m = re.search(r"\d+", str(raw))
    return int(m.group(0)) if m else None


def parse_direction_tag(raw: str | None) -> int | None:
    if not raw:
        return None
    s = raw.strip().upper()
    if s in CARDINAL_DEG:
        return CARDINAL_DEG[s]
    m = re.match(r"^(\d+(?:\.\d+)?)$", s)
    if m:
        return int(round(float(m.group(1)))) % 360
    return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return int(round((math.degrees(math.atan2(x, y)) + 360.0) % 360.0)) % 360


def alert_distance_for_speed(speed_kmh: int | None) -> int:
    if speed_kmh is None or speed_kmh <= 0:
        return 500
    if speed_kmh <= 40:
        return 250
    if speed_kmh <= 50:
        return 400
    if speed_kmh <= 60:
        return 550
    if speed_kmh <= 80:
        return 750
    if speed_kmh <= 90:
        return 900
    return 1050


def fov_for_highway(highway: str, lanes_raw: str | None) -> int:
    lanes = parse_speed(lanes_raw) or 0
    if highway in ("motorway", "motorway_link", "trunk", "trunk_link", "primary") or lanes >= 3:
        return 15
    return 35


def query_overpass(bbox: str, timeout: int = 90) -> dict:
    ql = f"""
[out:json][timeout:{timeout}];
(
  node["highway"="speed_camera"]({bbox});
  node["enforcement"~"^(maxspeed|traffic_signals|average_speed)$"]({bbox});
  relation["type"="enforcement"]["enforcement"~"^(maxspeed|traffic_signals|average_speed)$"]({bbox});
)->.cams;
(.cams; .cams >;)->.camnodes;
way(bn.camnodes)["highway"]->.parentways;
(
  .cams;
  .camnodes;
  .parentways;
);
out body;
>;
out skel qt;
""".strip()
    data = urllib.parse.urlencode({"data": ql}).encode("utf-8")
    last_err = None
    for attempt, backoff in enumerate((5, 15, 30), start=1):
        for url in OVERPASS_ENDPOINTS:
            req = urllib.request.Request(
                url,
                data=data,
                headers={"User-Agent": "OpenZeekr-BR-SpeedCameras/2.2"},
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout + 20) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                last_err = e
                print(f"[warn] attempt {attempt} {url} failed: {e}", file=sys.stderr)
        time.sleep(backoff)
    raise RuntimeError(f"All Overpass endpoints failed after retries: {last_err}")


def compute_way_heading(node_id: int, way: dict, nodes_by_id: dict[int, dict]) -> int | None:
    nds = way.get("nodes", [])
    try:
        idx = nds.index(node_id)
    except ValueError:
        return None
    prev_id = nds[idx - 1] if idx > 0 else None
    next_id = nds[idx + 1] if idx + 1 < len(nds) else None
    p_prev = nodes_by_id.get(prev_id) if prev_id is not None else None
    p_curr = nodes_by_id.get(node_id)
    p_next = nodes_by_id.get(next_id) if next_id is not None else None
    if p_prev and p_next:
        return bearing_deg(p_prev["lat"], p_prev["lon"], p_next["lat"], p_next["lon"])
    if p_prev and p_curr:
        return bearing_deg(p_prev["lat"], p_prev["lon"], p_curr["lat"], p_curr["lon"])
    if p_curr and p_next:
        return bearing_deg(p_curr["lat"], p_curr["lon"], p_next["lat"], p_next["lon"])
    return None


def extract_rows_from_overpass(raw: dict) -> list[dict]:
    elements = raw.get("elements", [])
    nodes_by_id: dict[int, dict] = {}
    ways_by_node: dict[int, list[dict]] = {}
    relations: list[dict] = []

    for el in elements:
        t = el.get("type")
        if t == "node":
            existing = nodes_by_id.get(el["id"])
            if existing is None or (el.get("tags") and not existing.get("tags")):
                nodes_by_id[el["id"]] = el
        elif t == "way":
            if "highway" in el.get("tags", {}):
                for nid in el.get("nodes", []):
                    ways_by_node.setdefault(nid, []).append(el)
        elif t == "relation":
            relations.append(el)

    rel_info_by_node: dict[int, dict] = {}
    for rel in relations:
        rtags = rel.get("tags", {})
        from_node = None
        to_node = None
        device_ids = []
        for mem in rel.get("members", []):
            if mem.get("type") != "node":
                continue
            role = mem.get("role", "")
            if role == "from":
                from_node = nodes_by_id.get(mem["ref"])
            elif role == "to":
                to_node = nodes_by_id.get(mem["ref"])
            elif role in ("device", ""):
                device_ids.append(mem["ref"])
        rel_heading = None
        if from_node and to_node:
            rel_heading = bearing_deg(from_node["lat"], from_node["lon"], to_node["lat"], to_node["lon"])
        # If no explicit device node, anchor on `from` node
        if not device_ids and from_node:
            device_ids.append(from_node["id"])
        for did in device_ids:
            entry = rel_info_by_node.setdefault(did, {"tags": {}, "heading": None, "from_id": None})
            entry["tags"].update(rtags)
            if rel_heading is not None:
                entry["heading"] = rel_heading
            if from_node:
                entry["from_id"] = from_node["id"]

    rows: list[dict] = []
    for nid, el in nodes_by_id.items():
        ntags = el.get("tags", {})
        rinfo = rel_info_by_node.get(nid)
        rtags = rinfo["tags"] if rinfo else {}
        is_cam = (
            ntags.get("highway") == "speed_camera"
            or ntags.get("enforcement") in ("maxspeed", "traffic_signals", "average_speed")
            or rinfo is not None
        )
        if not is_cam:
            continue

        # Look for parent way on this node or on the relation's `from` node
        parent_way = ways_by_node.get(nid, [None])[0]
        way_anchor = nid
        if parent_way is None and rinfo and rinfo["from_id"] is not None:
            parent_way = ways_by_node.get(rinfo["from_id"], [None])[0]
            way_anchor = rinfo["from_id"]
        wtags = parent_way.get("tags", {}) if parent_way else {}

        enf = ntags.get("enforcement") or rtags.get("enforcement") or "maxspeed"
        cam_type = {
            "traffic_signals": "redlight_speed",
            "average_speed": "average_speed",
        }.get(enf, "fixed_speed")

        speed = (
            parse_speed(ntags.get("maxspeed"))
            or parse_speed(rtags.get("maxspeed"))
            or parse_speed(wtags.get("maxspeed"))
        )
        road_name = (
            ntags.get("name")
            or wtags.get("name")
            or wtags.get("ref")
            or ntags.get("ref")
            or rtags.get("ref")
            or ""
        )
        highway_cls = wtags.get("highway", "")
        oneway_val = (wtags.get("oneway") or "").lower()
        is_oneway = oneway_val in ("yes", "1", "true", "-1") or highway_cls in ("motorway", "motorway_link", "trunk_link")

        explicit_dir = parse_direction_tag(ntags.get("direction") or rtags.get("direction"))
        rel_heading = rinfo["heading"] if rinfo else None
        way_heading = compute_way_heading(way_anchor, parent_way, nodes_by_id) if parent_way else None
        if way_heading is not None and oneway_val == "-1":
            way_heading = (way_heading + 180) % 360

        heading = explicit_dir if explicit_dir is not None else (rel_heading if rel_heading is not None else way_heading)
        bidirectional = (not is_oneway) and (explicit_dir is None) and (rel_heading is None)

        rows.append(
            {
                "osm_id": nid,
                "lat": round(float(el["lat"]), 6),
                "lon": round(float(el["lon"]), 6),
                "maxspeed_kmh": speed,
                "heading_deg": heading,
                "bidirectional": bidirectional,
                "fov_deg": fov_for_highway(highway_cls, wtags.get("lanes")),
                "alert_distance_m": alert_distance_for_speed(speed),
                "type": cam_type,
                "road_name": road_name,
                "source": "osm",
            }
        )
    return rows


def load_extra_csv(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        reader = csv.DictReader(f, dialect=dialect)
        for row in reader:
            norm = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            lat_s = norm.get("lat") or norm.get("latitude") or norm.get("y")
            lon_s = norm.get("lon") or norm.get("lng") or norm.get("longitude") or norm.get("x")
            if not lat_s or not lon_s:
                continue
            try:
                lat = float(lat_s.replace(",", "."))
                lon = float(lon_s.replace(",", "."))
            except ValueError:
                continue
            speed = parse_speed(
                norm.get("maxspeed_kmh")
                or norm.get("velocidade")
                or norm.get("velocidade_leve")
                or norm.get("maxspeed")
            )
            heading = parse_direction_tag(norm.get("heading_deg") or norm.get("direction") or norm.get("sentido"))
            rows.append(
                {
                    "osm_id": "",
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "maxspeed_kmh": speed,
                    "heading_deg": heading,
                    "bidirectional": heading is None,
                    "fov_deg": 20,
                    "alert_distance_m": alert_distance_for_speed(speed),
                    "type": norm.get("type") or "fixed_speed",
                    "road_name": norm.get("road_name") or norm.get("rodovia") or norm.get("ref") or "",
                    "source": path.stem,
                }
            )
    return rows


def deduplicate(rows: list[dict], radius_m: float = 40.0) -> list[dict]:
    buckets: dict[tuple[int, int], list[dict]] = {}
    kept: list[dict] = []
    for r in rows:
        lat, lon = r["lat"], r["lon"]
        bx, by = int(lat * 1000), int(lon * 1000)
        dup = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for existing in buckets.get((bx + dx, by + dy), ()):
                    if haversine_m(lat, lon, existing["lat"], existing["lon"]) <= radius_m:
                        dup = existing
                        break
                if dup:
                    break
            if dup:
                break
        if dup:
            if dup["maxspeed_kmh"] is None and r["maxspeed_kmh"] is not None:
                dup["maxspeed_kmh"] = r["maxspeed_kmh"]
                dup["alert_distance_m"] = alert_distance_for_speed(dup["maxspeed_kmh"])
            if dup["heading_deg"] is None and r["heading_deg"] is not None:
                dup["heading_deg"] = r["heading_deg"]
                dup["bidirectional"] = r["bidirectional"]
            if not dup["road_name"] and r["road_name"]:
                dup["road_name"] = r["road_name"]
            continue
        kept.append(r)
        buckets.setdefault((bx, by), []).append(r)
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Brazil fixed cameras from OSM (tiled) with heading/FOV/range.")
    parser.add_argument("--bbox", default=None, help="Optional single bbox south,west,north,east (default: 6 Brazil tiles)")
    parser.add_argument("--out-dir", default=".", help="Output directory for CSV/JSON")
    parser.add_argument("--merge-csv", action="append", default=[], help="Optional official CSV (DNIT/ANTT/DER)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tiles = [("custom", args.bbox)] if args.bbox else BRAZIL_TILES
    all_rows: list[dict] = []
    for name, bbox in tiles:
        print(f"Fetching tile {name} ({bbox}) ...")
        raw = query_overpass(bbox)
        t_rows = extract_rows_from_overpass(raw)
        print(f"  -> {len(t_rows)} raw cameras")
        all_rows.extend(t_rows)
        if len(tiles) > 1:
            time.sleep(2.0)

    for extra_p in args.merge_csv:
        p = Path(extra_p)
        extra_rows = load_extra_csv(p)
        print(f"Loaded {len(extra_rows)} rows from {p}")
        all_rows.extend(extra_rows)

    deduped = deduplicate(all_rows, radius_m=40.0)
    now_utc = dt.datetime.now(dt.timezone.utc)
    version = int(now_utc.strftime("%Y%m%d"))
    with_speed = sum(1 for r in deduped if r["maxspeed_kmh"] is not None)
    with_heading = sum(1 for r in deduped if r["heading_deg"] is not None)
    with_road = sum(1 for r in deduped if r["road_name"])

    csv_path = out_dir / "br_speed_cameras.csv"
    json_path = out_dir / "br_speed_cameras.json"
    fields = [
        "osm_id", "lat", "lon", "maxspeed_kmh", "heading_deg",
        "bidirectional", "fov_deg", "alert_distance_m", "type", "road_name", "source",
    ]

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(deduped)

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "version": version,
                "updated_at": now_utc.isoformat(),
                "source": "OpenStreetMap (ODbL 1.0) + optional official CSVs",
                "count": len(deduped),
                "with_maxspeed": with_speed,
                "with_heading": with_heading,
                "with_road_name": with_road,
                "cameras": deduped,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    print(
        f"Done! v{version}: {len(deduped)} deduplicated cameras "
        f"(speed={with_speed}, heading={with_heading}, road_name={with_road})"
    )
    gatso_path = out_dir / "GATSO_ALL.csv"
    with gatso_path.open("w", newline="", encoding="utf-8") as gf:
        gw = csv.writer(gf)
        for r in deduped:
            spd = r.get("maxspeed_kmh")
            spd_label = f"max @{spd}" if spd not in (None, "") else "max"
            gw.writerow([r["lon"], r["lat"], spd_label, f"BR Radar {r.get('type', 'fixed_speed')}"])

    zip_path = out_dir / "csv_files.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(gatso_path, arcname="GATSO_ALL.csv")
        zf.write(csv_path, arcname="br_speed_cameras.csv")
        attr = Path(__file__).parent / "ATTRIBUTION.md"
        if attr.exists():
            zf.write(attr, arcname="ATTRIBUTION.md")

    print(f"  CSV  : {csv_path.resolve()}")
    print(f"  GATSO: {gatso_path.resolve()}")
    print(f"  ZIP  : {zip_path.resolve()}")
    print(f"  JSON : {json_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
