"""Step 3 (Big Data): find close encounters between ships in the study area and classify them.

Usage:
    python -m src.big_data.encounters

Method (kept simple on purpose):
1. Keep moving ships inside the study area and take one position per ship per minute.
2. Put every position in a grid cell of about 1 nautical mile, and only compare ships that are in the same
   or a neighbouring cell at the same minute. This avoids comparing every ship with every other ship.
3. A pair of ships closer than ENCOUNTER_DISTANCE_M is "close". Consecutive close minutes of the same pair
   form one encounter.
4. Each encounter is classified from the two courses when it starts (COLREG rules 13-15, simplified):
   head-on if the courses differ by HEAD_ON_MIN_DEG or more, overtaking if they differ by OVERTAKING_MAX_DEG
   or less (same direction), crossing otherwise.

Writes DATA_DIR/analysis/encounters.parquet and reports/encounters_summary.csv.
"""
import duckdb

from ..common.clean import haversine_m
from ..common.config import CLEAN_DIR, DATA_DIR, REPORTS_DIR

# Øresund: the strait between Denmark and Sweden
AREA = {"name": "Oresund", "lat_min": 55.3, "lat_max": 56.1, "lon_min": 12.4, "lon_max": 13.0}

MIN_SPEED_KN = 2             # slower ships are moored, anchored or manoeuvring in port
ENCOUNTER_DISTANCE_M = 1852  # 1 nautical mile
MAX_GAP_MIN = 2              # a pair that is apart for longer than this starts a new encounter
HEAD_ON_MIN_DEG = 165
OVERTAKING_MAX_DEG = 67.5

# Ships whose job is to go close to other ships; their encounters are not collision-risk situations
SERVICE_TYPES = ("Tug", "Towing", "Pilot", "Dredging", "Port tender", "SAR", "Diving", "Law enforcement")

# Harbours where ships meet while berthing or on short harbour routes (lat_min, lat_max, lon_min, lon_max)
PORT_ZONES = {
    "Copenhagen harbour": (55.65, 55.725, 12.55, 12.65),
    "Helsingor ferry port": (56.030, 56.045, 12.600, 12.630),
    "Helsingborg ferry port": (56.035, 56.055, 12.680, 12.705),
}


def course_diff(a, b):
    """Smallest angle between two courses, 0-180 degrees."""
    return f"least(abs({a} - {b}), 360 - abs({a} - {b}))"


def relative_bearing(lat1, lon1, cog1, lat2, lon2):
    """Direction of ship 2 as seen from ship 1's bow, 0-360 degrees clockwise."""
    dlon = f"radians({lon2} - {lon1})"
    bearing = (f"degrees(atan2(sin({dlon}) * cos(radians({lat2})), "
               f"cos(radians({lat1})) * sin(radians({lat2})) - sin(radians({lat1})) * cos(radians({lat2})) * cos({dlon})))")
    return f"(({bearing} - {cog1}) % 360 + 360) % 360"


def main():
    out_dir = DATA_DIR / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET temp_directory = '{(DATA_DIR / 'tmp').as_posix()}'")
    a = AREA

    con.execute(f"""
        CREATE TABLE vessels AS
        SELECT mmsi,
               coalesce(mode(ship_type) FILTER (ship_type <> 'Unknown'), 'Unknown') AS ship_type,
               bool_or(suspect_track) AS suspect_track
        FROM read_parquet('{CLEAN_DIR.as_posix()}/*/vessels.parquet')
        GROUP BY mmsi
    """)

    # 1. One position per ship per minute, moving ships in the area, ships with broken tracks left out
    con.execute(f"""
        CREATE TABLE pts AS
        SELECT * FROM (
            SELECT mmsi, time_bucket(INTERVAL 1 minute, ts) AS minute,
                   arg_max(lat, ts) AS lat, arg_max(lon, ts) AS lon,
                   arg_max(sog, ts) AS sog, arg_max(cog, ts) AS cog
            FROM read_parquet('{CLEAN_DIR.as_posix()}/*/positions.parquet')
            WHERE lat BETWEEN {a['lat_min']} AND {a['lat_max']} AND lon BETWEEN {a['lon_min']} AND {a['lon_max']}
              AND mmsi NOT IN (SELECT mmsi FROM vessels WHERE suspect_track)
            GROUP BY mmsi, minute
        )
        WHERE sog >= {MIN_SPEED_KN}
    """)

    # 2. Grid cells of about 1 nm (a degree of longitude is shorter in the north, so use the northern edge)
    con.execute(f"""
        ALTER TABLE pts ADD COLUMN cy INTEGER; ALTER TABLE pts ADD COLUMN cx INTEGER;
        UPDATE pts SET cy = floor(lat * 60), cx = floor(lon * 60 * cos(radians({a['lat_max']})));
    """)

    # 3. Close pairs at the same minute
    dist = haversine_m("a.lat", "a.lon", "b.lat", "b.lon")
    con.execute(f"""
        CREATE TABLE close_pairs AS
        SELECT a.minute, a.mmsi AS mmsi_a, b.mmsi AS mmsi_b, {dist} AS dist_m,
               a.lat AS lat_a, a.lon AS lon_a, a.sog AS sog_a, a.cog AS cog_a,
               b.lat AS lat_b, b.lon AS lon_b, b.sog AS sog_b, b.cog AS cog_b
        FROM pts a JOIN pts b
          ON a.minute = b.minute AND a.mmsi < b.mmsi
         AND b.cy BETWEEN a.cy - 1 AND a.cy + 1 AND b.cx BETWEEN a.cx - 1 AND a.cx + 1
        WHERE {dist} < {ENCOUNTER_DISTANCE_M}
    """)

    # Consecutive close minutes of the same pair form one encounter
    con.execute(f"""
        CREATE TABLE close_numbered AS
        SELECT *, sum(new_enc) OVER (PARTITION BY mmsi_a, mmsi_b ORDER BY minute) AS enc_no
        FROM (
            SELECT *, CASE WHEN minute - lag(minute) OVER (PARTITION BY mmsi_a, mmsi_b ORDER BY minute)
                                <= INTERVAL {MAX_GAP_MIN} minute THEN 0 ELSE 1 END AS new_enc
            FROM close_pairs
        )
    """)

    # 4. One row per encounter: situation at the start, and the closest point
    con.execute(f"""
        CREATE TABLE encounters AS
        WITH e AS (
            SELECT mmsi_a, mmsi_b, enc_no,
                   min(minute) AS start_time, max(minute) AS end_time, count(*) AS close_minutes,
                   arg_min(dist_m, minute) AS start_dist_m,
                   min(dist_m) AS min_dist_m, arg_min(minute, dist_m) AS closest_time,
                   arg_min(lat_a, minute) AS lat_a, arg_min(lon_a, minute) AS lon_a,
                   arg_min(sog_a, minute) AS sog_a, arg_min(cog_a, minute) AS cog_a,
                   arg_min(lat_b, minute) AS lat_b, arg_min(lon_b, minute) AS lon_b,
                   arg_min(sog_b, minute) AS sog_b, arg_min(cog_b, minute) AS cog_b,
                   arg_min((lat_a + lat_b) / 2, dist_m) AS closest_lat,
                   arg_min((lon_a + lon_b) / 2, dist_m) AS closest_lon
            FROM close_numbered
            GROUP BY mmsi_a, mmsi_b, enc_no
        )
        SELECT row_number() OVER (ORDER BY start_time, mmsi_a, mmsi_b) AS encounter_id,
               e.*,
               {course_diff('cog_a', 'cog_b')} AS course_diff_deg,
               CASE WHEN {course_diff('cog_a', 'cog_b')} >= {HEAD_ON_MIN_DEG} THEN 'Head-on'
                    WHEN {course_diff('cog_a', 'cog_b')} <= {OVERTAKING_MAX_DEG} THEN 'Overtaking'
                    ELSE 'Crossing' END AS encounter_type,
               least(sog_a, sog_b) / greatest(sog_a, sog_b) AS speed_ratio,
               {relative_bearing('lat_a', 'lon_a', 'cog_a', 'lat_b', 'lon_b')} AS bearing_b_from_a_deg,
               {relative_bearing('lat_b', 'lon_b', 'cog_b', 'lat_a', 'lon_a')} AS bearing_a_from_b_deg,
               epoch(closest_time - start_time) / 60 AS minutes_to_closest,
               closest_time > start_time AS converging,
               va.ship_type AS ship_type_a, vb.ship_type AS ship_type_b,
               CAST(start_time AS DATE) AS date, hour(start_time) AS hour
        FROM e
        LEFT JOIN vessels va ON va.mmsi = e.mmsi_a
        LEFT JOIN vessels vb ON vb.mmsi = e.mmsi_b
    """)

    # Mark encounters that are not open-water collision-risk situations; they are kept but left out of the analysis
    service = ", ".join(f"'{t}'" for t in SERVICE_TYPES)
    zone_cases = "\n".join(
        f"WHEN closest_lat BETWEEN {la} AND {lb} AND closest_lon BETWEEN {oa} AND {ob} THEN '{name}'"
        for name, (la, lb, oa, ob) in PORT_ZONES.items())
    con.execute(f"""
        ALTER TABLE encounters ADD COLUMN excluded_reason VARCHAR;
        UPDATE encounters SET excluded_reason = CASE
            {zone_cases}
            WHEN ship_type_a IN ({service}) OR ship_type_b IN ({service}) THEN 'Service vessel'
            WHEN NOT converging THEN 'Not converging'
        END;
    """)
    con.execute(f"COPY encounters TO '{(out_dir / 'encounters.parquet').as_posix()}' (FORMAT parquet)")

    exclusions = con.execute("""
        SELECT coalesce(excluded_reason, 'Kept for analysis') AS reason, count(*) AS encounters
        FROM encounters GROUP BY 1 ORDER BY 2 DESC
    """).df()
    exclusions.to_csv(REPORTS_DIR / "encounters_exclusions.csv", index=False)

    summary = con.execute("""
        SELECT encounter_type, count(*) AS encounters,
               round(median(min_dist_m)) AS median_min_dist_m,
               round(median(sog_a + sog_b) / 2, 1) AS median_speed_kn,
               round(median(speed_ratio), 2) AS median_speed_ratio,
               round(median(course_diff_deg)) AS median_course_diff_deg,
               round(median(minutes_to_closest), 1) AS median_minutes_to_closest
        FROM encounters WHERE excluded_reason IS NULL GROUP BY 1 ORDER BY 2 DESC
    """).df()
    summary.to_csv(REPORTS_DIR / "encounters_summary.csv", index=False)

    stats = con.execute("""
        SELECT (SELECT count(*) FROM pts) AS minute_positions,
               (SELECT count(DISTINCT mmsi) FROM pts) AS ships,
               (SELECT count(*) FROM close_pairs) AS close_pair_minutes,
               (SELECT count(*) FROM encounters) AS encounters
    """).df()
    print(stats.to_string(index=False))
    print(exclusions.to_string(index=False))
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
