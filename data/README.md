# Data

| Source | What | Licence | Size used | How it reaches the agents |
|---|---|---|---|---|
| NYC TLC trip records (yellow taxi), via Zetaris public sample bucket `s3://zetaris-public-dataset/ny_taxi/taxi_trips/` | 500,000 real trips, 2024-07-27 11:57 to 2024-08-01 18:35 (first and last day partial) | NYC Open Data terms (public) | ~187 MB CSV, not committed | loaded into local Postgres `taxi-pg`, registered in Zetaris as datasource `nyc_taxi_pg` |
| NYC TLC taxi zones (`ny_taxi/taxi_zones/`) | 260 zones (LocationID, zone, borough) | public | 8 KB after dropping geometry | same Postgres |
| Open-Meteo historical weather archive | daily NYC weather 2024-07-01..09-30 (temp, precipitation, wind, WMO code) | CC BY 4.0 | 92 rows | Zetaris REST table `NYC_WEATHER_REST.daily_raw`, view `investigator.nyc_daily_weather` |

Regenerate everything with `python data/setup/setup_data.py` (idempotent; downloads into the gitignored `data/raw/`).

Notes / known quirks (the agents learn these too):
- The trip timestamps carry a `Z` suffix but are New York local time (the hourly profile only makes sense without a
  timezone shift), so the view uses them as-is.
- The bucket's other NY_TAXI tables with `_10000` in the file name (trips, weather_data, zones, surge_pricing) are
  synthetic, and the big ones (hvv 38 GB, yellow_taxi 4 GB) are too large to scan over the internet, so they are not used.
- No personal data: trips have no rider identifiers.
