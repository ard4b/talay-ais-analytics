"""Step 3 (Data Mining): cut each ship's track into one-hour windows and describe its movement in each window.

Usage:
    python -m src.data_mining.features

Method:
1. Take one position per ship per minute (whole Danish area, all 5 days). Ships with a broken track are left out.
2. A gap longer than SEGMENT_GAP_MIN starts a new segment; each segment is cut into WINDOW_MIN windows.
   Windows with fewer than MIN_POINTS minutes of data are dropped.
3. Each window becomes one row of movement features. Windows where the ship never moves (moored or anchored)
   are flagged as stationary; they say nothing about how a ship type moves and are left out of modelling.

Only movement is used. Ship type is kept as the label, MMSI only to keep a ship's windows together.
Identity, size and position (name, length, width, draught, latitude, longitude) are never features.

Writes DATA_DIR/analysis/windows.parquet and reports/data_mining/window_counts.csv.
"""
import duckdb

from ..common.clean import haversine_m
from ..common.config import CLEAN_DIR, DATA_DIR, REPORTS_DIR
from ..big_data.encounters import course_diff

CLASSES = ("Cargo", "Tanker", "Passenger", "Fishing")
SEGMENT_GAP_MIN = 30
WINDOW_MIN = 60
MIN_POINTS = 20
STOP_KN = 0.5             # slower counts as stopped
MOVING_KN = 2             # course over ground is unreliable below this speed
TURN_DEG_PER_MIN = 15     # a course change faster than this counts as a turn
SPEED_CHANGE_KN = 1       # a speed change larger than this (per minute) counts as a speed change

# Model inputs, in the order used by the models
FEATURES = [
    "speed_mean", "speed_median", "speed_min", "speed_max", "speed_std", "speed_iqr", "speed_cv",
    "accel_mean", "decel_mean", "accel_abs_max",
    "turn_rate_mean", "turn_rate_max", "course_circ_var", "turns_per_hour",
    "heading_offset_mean", "heading_rate_mean",
    "stop_ratio", "stop_starts",
    "distance_nm", "displacement_nm", "straightness",
    "speed_changes_per_hour",
]


def main():
    out_dir = DATA_DIR / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / "data_mining").mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET temp_directory = '{(DATA_DIR / 'tmp').as_posix()}'")

    labels = ", ".join(f"'{c}'" for c in CLASSES)
    con.execute(f"""
        CREATE TABLE vessels AS
        SELECT mmsi,
               coalesce(mode(ship_type) FILTER (ship_type <> 'Unknown'), 'Unknown') AS ship_type,
               bool_or(suspect_track) AS suspect_track
        FROM read_parquet('{CLEAN_DIR.as_posix()}/*/vessels.parquet')
        GROUP BY mmsi
        HAVING ship_type IN ({labels}, 'Unknown') AND NOT suspect_track
    """)

    # 1. One position per ship per minute
    con.execute(f"""
        CREATE TABLE minutes AS
        SELECT mmsi, time_bucket(INTERVAL 1 minute, ts) AS minute,
               arg_max(lat, ts) AS lat, arg_max(lon, ts) AS lon, arg_max(sog, ts) AS sog,
               arg_max(cog, ts) AS cog, arg_max(heading, ts) AS heading
        FROM read_parquet('{CLEAN_DIR.as_posix()}/*/positions.parquet')
        WHERE mmsi IN (SELECT mmsi FROM vessels)
        GROUP BY mmsi, minute
    """)

    # 2. Change from the previous minute, segments and windows
    step_m = haversine_m("prev_lat", "prev_lon", "lat", "lon")
    con.execute(f"""
        CREATE TABLE steps AS
        WITH p AS (
            SELECT *,
                   lag(minute) OVER w AS prev_minute, lag(lat) OVER w AS prev_lat, lag(lon) OVER w AS prev_lon,
                   lag(sog) OVER w AS prev_sog, lag(cog) OVER w AS prev_cog, lag(heading) OVER w AS prev_heading
            FROM minutes WINDOW w AS (PARTITION BY mmsi ORDER BY minute)
        ), g AS (
            SELECT *, epoch(minute - prev_minute) / 60 AS gap_min FROM p
        ), s AS (
            SELECT *, sum(CASE WHEN gap_min IS NULL OR gap_min > {SEGMENT_GAP_MIN} THEN 1 ELSE 0 END)
                          OVER (PARTITION BY mmsi ORDER BY minute) AS segment
            FROM g
        ), w AS (
            SELECT *, min(minute) OVER (PARTITION BY mmsi, segment) AS segment_start FROM s
        )
        SELECT mmsi, segment,
               floor(epoch(minute - segment_start) / 60 / {WINDOW_MIN}) AS window_no,
               minute, lat, lon, sog, cog, heading, prev_sog,
               -- the first row of a segment has no step inside the segment
               CASE WHEN gap_min <= {SEGMENT_GAP_MIN} THEN gap_min END AS dt_min,
               CASE WHEN gap_min <= {SEGMENT_GAP_MIN} THEN {step_m} END AS step_m,
               CASE WHEN gap_min <= {SEGMENT_GAP_MIN} THEN (sog - prev_sog) / gap_min END AS accel,
               CASE WHEN gap_min <= {SEGMENT_GAP_MIN} AND sog >= {MOVING_KN} AND prev_sog >= {MOVING_KN}
                    THEN {course_diff('cog', 'prev_cog')} / gap_min END AS turn_rate,
               CASE WHEN gap_min <= {SEGMENT_GAP_MIN} AND heading IS NOT NULL AND prev_heading IS NOT NULL
                    THEN {course_diff('heading', 'prev_heading')} / gap_min END AS heading_rate,
               CASE WHEN sog >= {MOVING_KN} AND heading IS NOT NULL THEN {course_diff('heading', 'cog')} END
                   AS heading_offset
        FROM w
    """)

    # 3. One row of features per window
    first_last = haversine_m("first_lat", "first_lon", "last_lat", "last_lon")
    con.execute(f"""
        CREATE TABLE windows AS
        WITH agg AS (
            SELECT mmsi, segment, window_no,
                   min(minute) AS window_start, count(*) AS points,
                   avg(sog) AS speed_mean, median(sog) AS speed_median, min(sog) AS speed_min, max(sog) AS speed_max,
                   coalesce(stddev_samp(sog), 0) AS speed_std,
                   quantile_cont(sog, 0.75) - quantile_cont(sog, 0.25) AS speed_iqr,
                   coalesce(avg(accel) FILTER (accel > 0), 0) AS accel_mean,
                   coalesce(-avg(accel) FILTER (accel < 0), 0) AS decel_mean,
                   coalesce(max(abs(accel)), 0) AS accel_abs_max,
                   avg(turn_rate) AS turn_rate_mean, max(turn_rate) AS turn_rate_max,
                   -- circular variance of the course while moving: 0 = one course, 1 = all directions
                   1 - sqrt(pow(avg(sin(radians(cog))) FILTER (sog >= {MOVING_KN}), 2)
                            + pow(avg(cos(radians(cog))) FILTER (sog >= {MOVING_KN}), 2)) AS course_circ_var,
                   count(*) FILTER (turn_rate > {TURN_DEG_PER_MIN}) AS turns,
                   avg(heading_offset) AS heading_offset_mean, avg(heading_rate) AS heading_rate_mean,
                   avg(CASE WHEN sog < {STOP_KN} THEN 1 ELSE 0 END) AS stop_ratio,
                   count(*) FILTER (prev_sog < {STOP_KN} AND sog >= {STOP_KN} AND dt_min IS NOT NULL) AS stop_starts,
                   coalesce(sum(step_m), 0) / 1852 AS distance_nm,
                   count(*) FILTER (abs(accel) * dt_min > {SPEED_CHANGE_KN}) AS speed_changes,
                   arg_min(lat, minute) AS first_lat, arg_min(lon, minute) AS first_lon,
                   arg_max(lat, minute) AS last_lat, arg_max(lon, minute) AS last_lon,
                   epoch(max(minute) - min(minute)) / 3600 AS hours
            FROM steps
            GROUP BY mmsi, segment, window_no
            HAVING count(*) >= {MIN_POINTS}
        )
        SELECT a.mmsi, v.ship_type, a.window_start, a.points,
               a.speed_max < {STOP_KN} AS stationary,
               speed_mean, speed_median, speed_min, speed_max, speed_std, speed_iqr,
               CASE WHEN speed_mean > 0 THEN speed_std / speed_mean ELSE 0 END AS speed_cv,
               accel_mean, decel_mean, accel_abs_max,
               turn_rate_mean, turn_rate_max, course_circ_var,
               turns / greatest(hours, 1 / 60) AS turns_per_hour,
               heading_offset_mean, heading_rate_mean,
               stop_ratio, stop_starts,
               distance_nm, {first_last} / 1852 AS displacement_nm,
               CASE WHEN distance_nm > 0 THEN least({first_last} / 1852 / distance_nm, 1) ELSE 0 END AS straightness,
               speed_changes / greatest(hours, 1 / 60) AS speed_changes_per_hour
        FROM agg a JOIN vessels v USING (mmsi)
        ORDER BY mmsi, window_start
    """)
    con.execute(f"COPY windows TO '{(out_dir / 'windows.parquet').as_posix()}' (FORMAT parquet)")

    counts = con.execute(f"""
        SELECT ship_type,
               count(DISTINCT mmsi) AS ships,
               count(*) AS windows,
               count(*) FILTER (stationary) AS stationary_windows,
               count(DISTINCT mmsi) FILTER (NOT stationary) AS ships_moving,
               count(*) FILTER (NOT stationary) AS moving_windows
        FROM windows GROUP BY 1 ORDER BY ships DESC
    """).df()
    counts.to_csv(REPORTS_DIR / "data_mining" / "window_counts.csv", index=False)
    print(con.execute("SELECT count(*) AS minute_positions FROM minutes").df().to_string(index=False))
    print(counts.to_string(index=False))


if __name__ == "__main__":
    main()
