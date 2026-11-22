# Brazil Open Speed Cameras (`Open-GATSO-POI` Mirror)

Automated daily open-data feed of fixed, red-light, and average-speed cameras across **Brazil**, built from **OpenStreetMap (ODbL 1.0)** and packaged in the `1e1/Open-GATSO-POI` release format (`csv_files.zip` → `GATSO_ALL.csv`) for **OpenZeekr** and **WirelessAAManipulator**.

## Release Format
Each daily release publishes `csv_files.zip` containing:
- `GATSO_ALL.csv`: Garmin-POI rows (`lon,lat,"max @<speed_kmh>","BR Radar <type>"`)
- `br_speed_cameras.csv`: Enriched dataset with `osm_id`, `lat`, `lon`, `maxspeed_kmh`, `heading_deg`, `bidirectional`, `fov_deg`, `alert_distance_m`, `type`, `road_name`, `source`
- `ATTRIBUTION.md`: OpenStreetMap ODbL 1.0 attribution

## Data License & Attribution
Map & camera data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, licensed under **ODbL 1.0**.
