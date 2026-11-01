"""Step 2: clean one day of AIS positions and record how many rows each rule removes.

Usage:
    python -m src.common.clean 2025-02-25 [2025-02-26 ...]

Reads PARQUET_DIR/date=YYYY-MM-DD/positions.parquet and writes to CLEAN_DIR/date=YYYY-MM-DD/:
    positions.parquet  cleaned ship positions (ts, mmsi, lat, lon, sog, cog, heading, nav_status)
    vessels.parquet    one row per ship with its ship type, name and size
The row counts per rule go to reports/cleaning_report.csv.
"""
import csv
import sys

import duckdb

from .config import CLEAN_DIR, DATA_DIR, PARQUET_DIR, REPORTS_DIR

MAX_SPEED_KN = 50       # faster than any ship in these waters
JUMP_MIN_DISTANCE_M = 500  # ignore small GPS jitter when looking for jumps
MS_TO_KN = 1.943844

# Ship type values that do not tell us what the ship is
UNKNOWN_TYPES = ("Undefined", "Reserved", "Other")


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres, as a SQL expression."""
    return (f"2 * 6371000 * asin(sqrt(pow(sin(radians({lat2} - {lat1}) / 2), 2)"
            f" + cos(radians({lat1})) * cos(radians({lat2})) * pow(sin(radians({lon2} - {lon1}) / 2), 2)))")


def count(con):
    return con.execute("SELECT count(*) FROM pos").fetchone()[0]


def build_vessels(con, src):
    """Ship type and size per MMSI, taken from all rows before any are removed."""
    unknown = ", ".join(f"'{t}'" for t in UNKNOWN_TYPES)
    con.execute(f"""
        CREATE TABLE vessels AS
        SELECT mmsi,
               coalesce(mode(ship_type) FILTER (ship_type NOT IN ({unknown})), 'Unknown') AS ship_type,
               mode(name) FILTER (name IS NOT NULL) AS name,
               median(length) AS length_m,
               median(width) AS width_m
        FROM '{src}'
        WHERE mobile_type IN ('Class A', 'Class B')
        GROUP BY mmsi
    """)


def remove_jumps(con):
    """Drop points that are far from both neighbours, i.e. the ship 'teleports' there and back."""
    prev_d = haversine_m("prev_lat", "prev_lon", "lat", "lon")
    next_d = haversine_m("lat", "lon", "next_lat", "next_lon")
    con.execute(f"""
        CREATE TABLE pos_next AS
        WITH n AS (
            SELECT *,
                   lag(lat) OVER w AS prev_lat, lag(lon) OVER w AS prev_lon,
                   epoch(ts - lag(ts) OVER w) AS prev_dt,
                   lead(lat) OVER w AS next_lat, lead(lon) OVER w AS next_lon,
                   epoch(lead(ts) OVER w - ts) AS next_dt
            FROM pos
            WINDOW w AS (PARTITION BY mmsi ORDER BY ts)
        ), s AS (
            SELECT *,
                   {prev_d} AS prev_m, {prev_d} / prev_dt * {MS_TO_KN} AS prev_kn,
                   {next_d} AS next_m, {next_d} / next_dt * {MS_TO_KN} AS next_kn
            FROM n
        )
        SELECT ts, mmsi, lat, lon, sog, cog, heading, nav_status
        FROM s
        WHERE NOT (
            coalesce(prev_kn > {MAX_SPEED_KN} AND prev_m > {JUMP_MIN_DISTANCE_M}, next_dt IS NOT NULL)
            AND coalesce(next_kn > {MAX_SPEED_KN} AND next_m > {JUMP_MIN_DISTANCE_M}, prev_dt IS NOT NULL)
            AND (prev_dt IS NOT NULL OR next_dt IS NOT NULL)
        )
    """)
    con.execute("DROP TABLE pos")
    con.execute("ALTER TABLE pos_next RENAME TO pos")


# (rule name, rows to keep) applied in this order
RULES = [
    ("Not a ship (base station, buoy, aircraft...)", "mobile_type IN ('Class A', 'Class B')"),
    ("Invalid MMSI", "mmsi BETWEEN 200000000 AND 799999999"),
    ("Position not available or out of range",
     "lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180 AND lat <> 91 AND lon <> 181"),
    ("Speed or course not available",
     "sog IS NOT NULL AND sog < 102.2 AND cog IS NOT NULL AND cog < 360"),
    (f"Speed above {MAX_SPEED_KN} knots", f"sog <= {MAX_SPEED_KN}"),
]


def clean_day(con, day):
    src = (PARQUET_DIR / f"date={day}" / "positions.parquet").as_posix()
    out_dir = CLEAN_DIR / f"date={day}"
    out_dir.mkdir(parents=True, exist_ok=True)

    build_vessels(con, src)
    con.execute(f"CREATE TABLE pos AS SELECT * FROM '{src}'")
    report = [("Raw rows", count(con), 0)]

    def step(name, before):
        after = count(con)
        report.append((name, after, before - after))
        print(f"{name}: -{before - after:,} -> {after:,}")
        return after

    n = count(con)
    # Same message received by several shore stations
    con.execute("CREATE TABLE pos_next AS SELECT DISTINCT * FROM pos")
    con.execute("DROP TABLE pos; ALTER TABLE pos_next RENAME TO pos")
    n = step("Exact duplicate rows", n)

    for name, keep in RULES:
        con.execute(f"DELETE FROM pos WHERE NOT ({keep})")
        n = step(name, n)

    # Two different reports for the same ship at the same second: keep one
    con.execute("""
        CREATE TABLE pos_next AS
        SELECT * FROM pos QUALIFY row_number() OVER (PARTITION BY mmsi, ts ORDER BY lat, lon) = 1
    """)
    con.execute("DROP TABLE pos; ALTER TABLE pos_next RENAME TO pos")
    n = step("Same ship, same second", n)

    # Removing one bad point can expose the next one (two bad points in a row), so repeat a few times
    before = n
    for _ in range(5):
        remove_jumps(con)
        if count(con) == before:
            break
        before = count(con)
    n = step("Position jumps", n)

    # A one-sided jump (the track breaks and continues elsewhere) usually means two ships share an
    # MMSI or the signal was lost for a long time. Too many rows to drop, so flag those ships instead.
    step_d = haversine_m("prev_lat", "prev_lon", "lat", "lon")
    suspect = con.execute(f"""
        CREATE TABLE suspect AS
        WITH n AS (
            SELECT mmsi, lat, lon, lag(lat) OVER w AS prev_lat, lag(lon) OVER w AS prev_lon,
                   epoch(ts - lag(ts) OVER w) AS dt
            FROM pos WINDOW w AS (PARTITION BY mmsi ORDER BY ts)
        )
        SELECT DISTINCT mmsi FROM n
        WHERE dt IS NOT NULL AND {step_d} > {JUMP_MIN_DISTANCE_M} AND {step_d} / dt * {MS_TO_KN} > {MAX_SPEED_KN};
        SELECT count(*) FROM suspect
    """).fetchone()[0]
    report.append((f"Ships with a broken track (flagged, kept): {suspect}", n, 0))
    print(f"Ships with a broken track (flagged, kept): {suspect}")

    con.execute(f"COPY (SELECT * FROM pos ORDER BY mmsi, ts) TO '{(out_dir / 'positions.parquet').as_posix()}' "
                "(FORMAT parquet, COMPRESSION zstd)")
    con.execute(f"""
        COPY (SELECT v.*, v.mmsi IN (SELECT mmsi FROM suspect) AS suspect_track
              FROM vessels v WHERE mmsi IN (SELECT DISTINCT mmsi FROM pos))
        TO '{(out_dir / 'vessels.parquet').as_posix()}' (FORMAT parquet)
    """)
    con.execute("DROP TABLE suspect")
    con.execute("DROP TABLE pos; DROP TABLE vessels")
    return report


def save_report(day, report):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / "cleaning_report.csv"
    existing = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            existing = [r for r in csv.DictReader(f) if r["date"] != day]
    raw = report[0][1]
    rows = [{"date": day, "step": name, "rows_after": after, "rows_removed": removed,
             "removed_pct_of_raw": round(100 * removed / raw, 2)} for name, after, removed in report]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(existing + rows)


def main(days):
    con = duckdb.connect()
    con.execute(f"SET temp_directory = '{(DATA_DIR / 'tmp').as_posix()}'")
    for day in days:
        print(f"== {day}")
        save_report(day, clean_day(con, day))


if __name__ == "__main__":
    main(sys.argv[1:])
