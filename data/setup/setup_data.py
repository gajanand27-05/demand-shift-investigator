"""One-command data setup: real NYC TLC trips + zones into a local Postgres, registered in Zetaris, plus
Open-Meteo weather as a Zetaris REST table, plus the analysis views. Idempotent; safe to re-run.

    python data/setup/setup_data.py            # needs Docker + the Zetaris stack running, and code/.env filled in

Steps (each skipped if already done):
  1. download taxi_trips (2 CSV parts, ~187 MB) + taxi_zones from the public bucket s3://zetaris-public-dataset
  2. start Postgres container `taxi-pg` on the `zetaris_default` Docker network, load trips + zones
  3. register it in Zetaris (CREATE DATASOURCE nyc_taxi_pg + REGISTER DATASOURCE TABLES)
  4. register Open-Meteo NYC daily weather (CREATE LIGHTNING REST TABLE) and the views in container `investigator`
"""
import csv
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent.tools_zetaris import run_sql  # noqa: E402

SETUP = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "raw"          # gitignored download cache
BUCKET = "https://zetaris-public-dataset.s3.amazonaws.com/ny_taxi"
FILES = {
    "trips_0.csv": "taxi_trips/part-00000-313f57b3-da5f-4533-a635-5eac19367cc1-c000.csv",
    "trips_1.csv": "taxi_trips/part-00001-313f57b3-da5f-4533-a635-5eac19367cc1-c000.csv",
    "taxi_zones.csv": "taxi_zones/part-00000-52358ee1-4b95-4313-be24-3222c74fbb1f-c000.csv",
}
PG = ["docker", "exec", "-i", "taxi-pg", "psql", "-q", "-v", "ON_ERROR_STOP=1", "-U", "taxi", "-d", "taxi"]


def step(msg):
    print(f"\n== {msg}", flush=True)


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def download():
    step("1/4 download real TLC trips + zones")
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, key in FILES.items():
        dest = CACHE / name
        if dest.exists() and dest.stat().st_size > 1000:
            print(f"   {name}: cached")
            continue
        print(f"   {name}: downloading ...", flush=True)
        urllib.request.urlretrieve(f"{BUCKET}/{key}", dest)
    zones = CACHE / "zones_small.csv"
    csv.field_size_limit(10 ** 9)
    seen = {}
    for r in csv.DictReader(open(CACHE / "taxi_zones.csv", encoding="utf-8")):
        seen.setdefault(r["LocationID"], (r["zone"], r["borough"]))
    with open(zones, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([k, *v] for k, v in seen.items())
    print(f"   {len(seen)} zones")


def postgres():
    step("2/4 Postgres container taxi-pg on the zetaris_default network")
    names = sh(["docker", "ps", "-a", "--format", "{{.Names}}"]).stdout.split()
    if "taxi-pg" not in names:
        sh(["docker", "run", "-d", "--name", "taxi-pg", "--network", "zetaris_default", "--restart", "unless-stopped",
            "-e", "POSTGRES_USER=taxi", "-e", "POSTGRES_PASSWORD=taxi_local_only", "-e", "POSTGRES_DB=taxi",
            "-p", "127.0.0.1:55432:5432", "postgres:16-alpine"])
    else:
        sh(["docker", "start", "taxi-pg"])
    for _ in range(30):
        if subprocess.run(["docker", "exec", "taxi-pg", "pg_isready", "-U", "taxi"], capture_output=True).returncode == 0:
            break
        time.sleep(1)
    n = sh(PG + ["-tAc", "SELECT to_regclass('trips_raw') IS NOT NULL AND (SELECT count(*) FROM trips_raw) > 0"],
           ).stdout.strip()
    if n == "t":
        print("   trips already loaded")
        return
    sh(PG, input=(SETUP / "01_taxi_schema.sql").read_text())
    with open(CACHE / "zones_small.csv", encoding="utf-8") as f:
        sh(PG + ["-c", "\\copy zones FROM STDIN WITH (FORMAT csv)"], stdin=f)
    for part in ("trips_0.csv", "trips_1.csv"):
        with open(CACHE / part, encoding="utf-8") as f:
            sh(PG + ["-c", "\\copy trips_raw FROM STDIN WITH (FORMAT csv, HEADER true)"], stdin=f)
    sh(PG + ["-c", "CREATE INDEX IF NOT EXISTS trips_pickup ON trips_raw (pickup_datetime); ANALYZE trips_raw;"])
    print("   " + sh(PG + ["-tAc", "SELECT count(*) || ' trips, ' || min(pickup_datetime) || ' .. ' || max(pickup_datetime) FROM trips_raw"]).stdout.strip())


def statements(path):
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("--")]


def zetaris(files, label):
    step(label)
    for path in files:
        for s in statements(path):
            try:
                run_sql(s)
                print(f"   ok   {s[:90]}")
            except Exception as e:
                msg = str(e)
                if "exist" in msg.lower():   # idempotent re-run
                    print(f"   skip {s[:90]} (already exists)")
                else:
                    raise


def check():
    rows = run_sql("SELECT pickup_date, COUNT(*) FROM investigator.trips GROUP BY 1 ORDER BY 1")["rows"]
    wx = run_sql("SELECT COUNT(*) FROM investigator.nyc_daily_weather")["rows"][0][0]
    print(f"\nReady: {sum(int(r[1]) for r in rows)} trips over {len(rows)} days, {wx} weather days, all queryable in Zetaris.")


if __name__ == "__main__":
    download()
    postgres()
    zetaris([SETUP / "02_zetaris_register.sql"], "3/4 register Postgres in Zetaris")
    zetaris([SETUP / "03_zetaris_weather.sql", SETUP / "04_zetaris_views.sql"], "4/4 weather REST table + views")
    check()
