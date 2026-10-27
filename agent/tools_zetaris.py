"""Data layer: every fact the agents use comes from Zetaris Lightning SQL over the local REST API.

Guardrails: read-only statements only, allow-listed tables (no full scans of the S3-backed NY_TAXI tables),
an enforced row LIMIT, an HTTP timeout, and a replay cache for SAMPLE_MODE.
"""
import os
import re
import time
import uuid

import requests
from dotenv import load_dotenv

from agent.llm import RUN, Cache, _cache, sample_mode, seq_key

load_dotenv()

TIMEOUT_S = float(os.environ.get("ZETARIS_TIMEOUT_S", "90"))
MAX_ROWS = 200
ALLOWED_TABLES = {"investigator.trips", "investigator.nyc_daily_weather", "nyc_taxi_pg.zones"}

CATALOG = """Zetaris tables you may query (Spark SQL dialect; dates as 'YYYY-MM-DD' strings):

1. investigator.trips  - one row per real NYC TLC taxi trip (source: Postgres datasource nyc_taxi_pg via Zetaris)
   trip_id, pickup_ts (timestamp, New York local), pickup_date (date), pickup_hour (0-23), pickup_dow ('Monday'..),
   pickup_location_id, pickup_zone, pickup_borough, dropoff_location_id, dropoff_zone, dropoff_borough,
   passengers, distance_mi, duration_min, speed_mph, fare_usd, tip_usd, total_usd,
   congestion_surcharge, airport_fee, is_valid_trip (boolean)
2. investigator.nyc_daily_weather - real daily NYC weather (source: Open-Meteo REST API registered in Zetaris)
   day (date), temp_max_c, temp_min_c, precip_mm, rain_mm, snow_cm, wind_max_kmh, wmo_code (WMO weather code)
   Join on investigator.trips.pickup_date = investigator.nyc_daily_weather.day
3. nyc_taxi_pg.zones - location_id, zone, borough  (260 NYC taxi zones)

Rules: one SELECT per call; always aggregate (GROUP BY) rather than returning raw trips; results are capped at 200 rows.
Use LIMIT n (Spark SQL), never FETCH FIRST n ROWS ONLY or TOP n."""


class SqlRejected(ValueError):
    pass


def normalise(sql: str) -> str:
    """Rewrite ANSI/T-SQL row limits Zetaris's Spark dialect rejects (FETCH FIRST n ROWS ONLY, TOP n) into LIMIT n."""
    s = re.sub(r"\s+FETCH\s+(?:FIRST|NEXT)\s+(\d+)\s+ROWS?\s+ONLY\s*$", r" LIMIT \1", sql.strip().rstrip(";"), flags=re.I)
    m = re.match(r"(?is)^\s*SELECT\s+TOP\s+(\d+)\s+(.*)$", s)
    if m and not re.search(r"LIMIT\s+\d+\s*$", s, re.I):
        s = f"SELECT {m.group(2)} LIMIT {m.group(1)}"
    return s


def guard(sql: str) -> str:
    s = normalise(sql).strip()
    low = s.lower()
    if not re.match(r"^(select|with)\b", low):
        raise SqlRejected("only SELECT/WITH queries are allowed")
    if ";" in s:
        raise SqlRejected("one statement per call")
    if re.search(r"\b(insert|update|delete|drop|create|alter|register|grant|truncate)\b", low):
        raise SqlRejected("write/DDL keywords are not allowed")
    refs = set(re.findall(r"\b(?:from|join)\s+([a-z_][\w]*\.[a-z_][\w]*)", low))
    bad = refs - ALLOWED_TABLES
    if bad:
        raise SqlRejected(f"tables not allowed: {sorted(bad)}; use only {sorted(ALLOWED_TABLES)}")
    if not refs:
        raise SqlRejected("query must read from one of the allowed tables")
    return s


def _post(sql: str, limit: int) -> dict:
    url = os.environ["ZETARIS_REST_URL"].rstrip("/") + "/sql-editor/sqls/run"
    response = requests.post(
        url,
        headers={
            "Authorization": "Bearer " + os.environ["ZETARIS_API_KEY"],
            "X-Org-ID": os.environ.get("ZETARIS_ORG_ID", "1"),
            "X-Request-ID": str(uuid.uuid4()),
        },
        json={"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit},
        timeout=TIMEOUT_S,
    )
    if response.status_code >= 400:
        try:
            msg = response.json().get("msg", response.text)
        except ValueError:
            msg = response.text
        raise RuntimeError(f"Zetaris {response.status_code}: {msg[:400]}")
    body = response.json()
    return {"columns": body.get("headers") or [], "rows": body.get("data") or []}


def run_sql(sql: str, limit: int = MAX_ROWS) -> dict:
    """Run one SQL statement in Zetaris (no guard; used by setup scripts)."""
    return _post(sql.strip().rstrip(";"), limit)


def query(sql: str) -> dict:
    """Agent tool: guarded, cached Zetaris query -> {"columns","rows","elapsed_s","cached","sql"}."""
    s = guard(sql)
    k = Cache.key("sql", s)
    sk = seq_key("sql")
    if sample_mode():
        hit = _cache.get(sk) or _cache.get(k)
        if hit is None:
            raise RuntimeError("SAMPLE_MODE: this query was not recorded")
        return {**hit, "cached": True}
    t0 = time.time()
    out = _post(s, MAX_ROWS)
    out.update(sql=s, elapsed_s=round(time.time() - t0, 2))
    _cache.put(k, out)
    if RUN["id"]:
        _cache.put(sk, out)
    return {**out, "cached": False}


def preview(result: dict, max_rows: int = 25) -> str:
    """Compact text table for prompts: agents pass summaries, not full result sets."""
    cols, rows = result["columns"], result["rows"]
    lines = [" | ".join(cols)] + [" | ".join(str(v) for v in r) for r in rows[:max_rows]]
    if len(rows) > max_rows:
        lines.append(f"... ({len(rows) - max_rows} more rows)")
    return "\n".join(lines)
