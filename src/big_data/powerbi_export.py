"""Step 5 (Big Data): prepare the tables for the Power BI dashboard.

Usage:
    python -m src.big_data.powerbi_export

Run after encounters and compare_talay. Writes one Excel workbook, reports/powerbi/talay_ais_dashboard.xlsx, with
one sheet per table. Excel keeps the column types, so Power BI does not have to guess decimal separators or date
formats from text (a common problem with CSV files on a Turkish-language Windows).

Sheets:
    summary            one row of headline numbers for the KPI cards
    cleaning_steps     rows removed by each cleaning rule (5 days together)
    format_comparison  CSV vs Parquet size and query time per day
    ship_types         ships per ship type in the whole dataset and in Øresund
    hourly_traffic     ships in Øresund per hour and ship group
    traffic_grid       ship-minutes per ~1 km cell in Øresund, for the traffic map
    encounters         every encounter found, with the reason when it is left out of the analysis
    encounter_ships    the two ships of each encounter (one row per ship)
    scenario_points    real encounters and Talay conditions on the same scale-free values, for the scatter chart
    talay_vs_real      Talay's value against the real P10 / median / P90 per condition and metric
"""
import duckdb
import pandas as pd

from ..common.config import CLEAN_DIR, DATA_DIR, REPORTS_DIR
from .compare_talay import CLOSE_CALL_M, talay_conditions
from .encounters import AREA, SERVICE_TYPES

OUT_PATH = REPORTS_DIR / "powerbi" / "talay_ais_dashboard.xlsx"
CELL_LAT_DEG = 0.01    # about 1.1 km
CELL_LON_DEG = 0.0175  # about 1.1 km at 56° N

# Fewer, broader groups than the 20+ AIS ship types, so charts stay readable
SHIP_GROUPS = {
    "Cargo": "Cargo", "Tanker": "Tanker",
    "Passenger": "Passenger", "HSC": "Passenger",
    "Fishing": "Fishing",
    "Pleasure": "Leisure", "Sailing": "Leisure",
    "Military": "Military",
    "Unknown": "Unknown",
    **{t: "Service" for t in SERVICE_TYPES + ("Anti-pollution", "Medical", "Towing long/wide")},
}
DISTANCE_BANDS = [(250, "< 250 m"), (500, "250-500 m"), (1000, "500-1000 m"), (1852, "1000-1852 m")]
METRIC_LABELS = {"course_diff_deg": "Course angle (°)", "speed_ratio": "Speed ratio",
                 "bearing_deg": "Other ship's direction (°)"}


def ship_group_sql(column):
    cases = " ".join(f"WHEN '{t}' THEN '{g}'" for t, g in SHIP_GROUPS.items())
    return f"CASE {column} {cases} ELSE 'Other' END"


def distance_band_sql(column):
    cases = " ".join(f"WHEN {column} < {limit} THEN '{label}'" for limit, label in DISTANCE_BANDS)
    return f"CASE {cases} END"


def load_views(con):
    a = AREA
    con.execute(f"""
        CREATE VIEW vessels AS
        SELECT mmsi,
               coalesce(mode(ship_type) FILTER (ship_type <> 'Unknown'), 'Unknown') AS ship_type,
               bool_or(suspect_track) AS suspect_track
        FROM read_parquet('{CLEAN_DIR.as_posix()}/*/vessels.parquet')
        GROUP BY mmsi
    """)
    # One position per ship per minute in Øresund (same sampling as the encounter detection, slow ships included)
    con.execute(f"""
        CREATE TABLE area_minutes AS
        SELECT p.*, v.ship_type, {ship_group_sql('v.ship_type')} AS ship_group
        FROM (
            SELECT mmsi, time_bucket(INTERVAL 1 minute, ts) AS minute,
                   arg_max(lat, ts) AS lat, arg_max(lon, ts) AS lon, arg_max(sog, ts) AS sog
            FROM read_parquet('{CLEAN_DIR.as_posix()}/*/positions.parquet')
            WHERE lat BETWEEN {a['lat_min']} AND {a['lat_max']} AND lon BETWEEN {a['lon_min']} AND {a['lon_max']}
            GROUP BY mmsi, minute
        ) p
        JOIN vessels v USING (mmsi)
    """)
    con.execute(f"CREATE VIEW enc AS SELECT * FROM '{(DATA_DIR / 'analysis' / 'encounters.parquet').as_posix()}'")


def cleaning_steps():
    report = pd.read_csv(REPORTS_DIR / "cleaning_report.csv")
    raw = report.loc[report.step == "Raw rows", "rows_after"].sum()
    steps = report[(report.step != "Raw rows") & ~report.step.str.startswith("Ships with a broken track")]
    steps = (steps.groupby("step", sort=False)["rows_removed"].sum().reset_index())
    steps.insert(0, "step_order", range(1, len(steps) + 1))
    steps["removed_pct_of_raw"] = (100 * steps.rows_removed / raw).round(2)
    return steps


def format_comparison():
    stats = pd.read_csv(REPORTS_DIR / "ingest_stats.csv").sort_values("date")
    long = pd.concat([
        pd.DataFrame({"date": stats.date, "format": "CSV", "size_mb": stats.csv_mb, "query_s": stats.csv_query_s}),
        pd.DataFrame({"date": stats.date, "format": "Parquet", "size_mb": stats.parquet_mb,
                      "query_s": stats.parquet_query_s}),
    ])
    long["date"] = pd.to_datetime(long.date)
    return long


def summary(con, cleaning):
    report = pd.read_csv(REPORTS_DIR / "cleaning_report.csv")
    counts = con.execute(f"""
        SELECT (SELECT count(*) FROM vessels) AS ships,
               (SELECT count(*) FROM vessels WHERE ship_type = 'Unknown') AS ships_unknown_type,
               (SELECT count(DISTINCT mmsi) FROM area_minutes) AS ships_in_oresund,
               (SELECT count(*) FROM enc) AS encounters_found,
               (SELECT count(*) FROM enc WHERE excluded_reason IS NULL) AS encounters_kept,
               (SELECT count(*) FROM enc WHERE excluded_reason IS NULL AND min_dist_m < {CLOSE_CALL_M}) AS close_calls
    """).df()
    counts.insert(0, "days", report.date.nunique())
    counts.insert(1, "raw_rows", report.loc[report.step == "Raw rows", "rows_after"].sum())
    counts.insert(2, "clean_rows", report.loc[report.step == "Position jumps", "rows_after"].sum())
    duplicates = cleaning.loc[cleaning.step == "Exact duplicate rows", "removed_pct_of_raw"].iloc[0]
    counts.insert(3, "duplicate_pct", duplicates)
    return counts


def ship_types(con):
    return con.execute(f"""
        SELECT v.ship_type, {ship_group_sql('v.ship_type')} AS ship_group,
               count(*) AS ships,
               count(*) FILTER (v.mmsi IN (SELECT mmsi FROM area_minutes)) AS ships_in_oresund
        FROM vessels v GROUP BY ALL ORDER BY ships DESC
    """).df()


def hourly_traffic(con):
    # "ships" counts distinct ships in that hour: add it up across ship groups, but not across hours
    return con.execute("""
        SELECT time_bucket(INTERVAL 1 hour, minute) AS hour_start,
               CAST(minute AS DATE) AS date, hour(minute) AS hour, ship_group,
               count(DISTINCT mmsi) AS ships,
               count(DISTINCT mmsi) FILTER (sog >= 2) AS moving_ships,
               count(*) AS ship_minutes
        FROM area_minutes GROUP BY ALL ORDER BY hour_start, ship_group
    """).df()


def traffic_grid(con):
    return con.execute(f"""
        SELECT round((floor(lat / {CELL_LAT_DEG}) + 0.5) * {CELL_LAT_DEG}, 4) AS cell_lat,
               round((floor(lon / {CELL_LON_DEG}) + 0.5) * {CELL_LON_DEG}, 4) AS cell_lon,
               ship_group,
               count(*) AS ship_minutes,
               count(*) FILTER (sog >= 2) AS moving_ship_minutes,
               count(DISTINCT mmsi) AS ships
        FROM area_minutes GROUP BY ALL ORDER BY cell_lat, cell_lon, ship_group
    """).df()


def encounters(con):
    return con.execute(f"""
        SELECT encounter_id, date, dayname(date) AS weekday, hour, start_time, closest_time,
               encounter_type,
               coalesce(excluded_reason, 'Kept for analysis') AS status,
               excluded_reason IS NULL AS kept,
               min_dist_m < {CLOSE_CALL_M} AS close_call,
               round(min_dist_m) AS min_dist_m,
               {distance_band_sql('min_dist_m')} AS distance_band,
               close_minutes, minutes_to_closest,
               round(course_diff_deg, 1) AS course_diff_deg,
               round(speed_ratio, 3) AS speed_ratio,
               -- direction of the other ship seen from the faster ship's bow, 0-180 (as in compare_talay)
               round(least(b, 360 - b), 1) AS bearing_deg,
               round((sog_a + sog_b) / 2, 1) AS mean_speed_kn,
               closest_lat, closest_lon,
               ship_type_a, ship_type_b,
               {ship_group_sql('ship_type_a')} AS ship_group_a, {ship_group_sql('ship_type_b')} AS ship_group_b
        FROM (SELECT *, CASE WHEN sog_a >= sog_b THEN bearing_b_from_a_deg ELSE bearing_a_from_b_deg END AS b FROM enc)
        ORDER BY encounter_id
    """).df()


def encounter_ships(con):
    return con.execute(f"""
        SELECT encounter_id, 'A' AS ship, mmsi_a AS mmsi, ship_type_a AS ship_type,
               {ship_group_sql('ship_type_a')} AS ship_group, sog_a AS speed_kn
        FROM enc
        UNION ALL
        SELECT encounter_id, 'B', mmsi_b, ship_type_b, {ship_group_sql('ship_type_b')}, sog_b
        FROM enc
        ORDER BY encounter_id, ship
    """).df()


def scenario_points(enc):
    real = enc[enc.kept][["encounter_type", "course_diff_deg", "speed_ratio", "bearing_deg", "close_call"]].copy()
    real.insert(0, "source", "Real encounter")
    real.insert(1, "label", "")
    talay = talay_conditions()
    talay = pd.DataFrame({
        "source": "Talay scenario", "label": talay.condition_id, "encounter_type": talay.encounter_type,
        "course_diff_deg": talay.course_diff_deg, "speed_ratio": talay.speed_ratio.round(3),
        "bearing_deg": talay.bearing_deg, "close_call": True,
    })
    return pd.concat([real, talay], ignore_index=True)


def talay_vs_real():
    result = pd.read_csv(REPORTS_DIR / "talay_vs_real.csv")
    result.insert(4, "metric_label", result.metric.map(METRIC_LABELS))
    return result


def main():
    con = duckdb.connect()
    con.execute(f"SET temp_directory = '{(DATA_DIR / 'tmp').as_posix()}'")
    load_views(con)

    cleaning = cleaning_steps()
    enc = encounters(con)
    sheets = {
        "summary": summary(con, cleaning),
        "cleaning_steps": cleaning,
        "format_comparison": format_comparison(),
        "ship_types": ship_types(con),
        "hourly_traffic": hourly_traffic(con),
        "traffic_grid": traffic_grid(con),
        "encounters": enc,
        "encounter_ships": encounter_ships(con),
        "scenario_points": scenario_points(enc),
        "talay_vs_real": talay_vs_real(),
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUT_PATH) as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
            print(f"{name}: {len(df):,} rows")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
