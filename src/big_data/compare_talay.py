"""Step 4 (Big Data): compare Talay's test scenarios with the real encounters found in Øresund.

Usage:
    python -m src.big_data.compare_talay

Talay's scenarios are small (ships start 75-150 m apart, camera range), while real encounters start at
1 nautical mile. Distances and times therefore cannot be compared directly. We compare values that do not
depend on scale:
    course_diff_deg   angle between the two courses (0 = same direction, 180 = opposite)
    speed_ratio       slower ship's speed / faster ship's speed
    bearing_deg       direction of the other ship seen from the faster ship's bow (0 = dead ahead, 90 = abeam),
                      folded to 0-180 so port and starboard count the same
In Talay the own ship (6 m/s) is always the faster one, so the real encounters are seen from the faster ship too.

Talay's targets are placed on a collision course on purpose, while most real encounters are ships passing at a
safe distance. So each scenario is compared with all real encounters of its type and, separately, with the
close calls only (closest distance under CLOSE_CALL_M).

Writes reports/talay_scenarios.csv and reports/talay_vs_real.csv.
"""
import json
import math

import duckdb
import pandas as pd

from ..common.config import DATA_DIR, REPO_DIR, REPORTS_DIR

SCENARIO_DIR = REPO_DIR / "talay_scenarios"
FAMILY_TO_TYPE = {"head_on": "Head-on", "crossing": "Crossing", "overtaking": "Overtaking"}
METRICS = ["course_diff_deg", "speed_ratio", "bearing_deg"]
CLOSE_CALL_M = 500


def talay_conditions():
    """One row per scenario condition with a moving target vessel."""
    rows = []
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        family = scenario["family"]
        if family not in FAMILY_TO_TYPE:
            continue  # static obstacles have no counterpart in AIS data
        for c in scenario["conditions"]:
            own, target = c["own"], c["targets"][0]
            # Unity: x = east, z = north, heading 0 = +z
            dx = target["start"]["x"] - own["start"]["x"]
            dz = target["start"]["z"] - own["start"]["z"]
            bearing = (math.degrees(math.atan2(dx, dz)) - own["headingDeg"]) % 360
            diff = abs(own["headingDeg"] - target["headingDeg"]) % 360
            rows.append({
                "condition_id": c["conditionId"],
                "encounter_type": FAMILY_TO_TYPE[family],
                "visibility": c.get("visibility", "clear"),
                "own_speed_mps": own["cruiseSpeedMps"],
                "target_speed_mps": target["speedMps"],
                "start_distance_m": round(math.hypot(dx, dz), 1),
                "course_diff_deg": min(diff, 360 - diff),
                "speed_ratio": (min(own["cruiseSpeedMps"], target["speedMps"])
                                / max(own["cruiseSpeedMps"], target["speedMps"])),
                "bearing_deg": round(min(bearing, 360 - bearing), 1),
                "description": c["description"],
            })
    return pd.DataFrame(rows)


def real_encounters(con):
    """Kept encounters, seen from the faster ship."""
    return con.execute(f"""
        SELECT encounter_type, min_dist_m, course_diff_deg, speed_ratio,
               least(b, 360 - b) AS bearing_deg
        FROM (
            SELECT *, CASE WHEN sog_a >= sog_b THEN bearing_b_from_a_deg ELSE bearing_a_from_b_deg END AS b
            FROM '{(DATA_DIR / 'analysis' / 'encounters.parquet').as_posix()}'
            WHERE excluded_reason IS NULL
        )
    """).df()


def compare(talay, real):
    groups = {
        "All encounters": real,
        f"Close calls (< {CLOSE_CALL_M} m)": real[real.min_dist_m < CLOSE_CALL_M],
    }
    rows = []
    for group, group_real in groups.items():
        for _, cond in talay.iterrows():
            same_type = group_real[group_real.encounter_type == cond.encounter_type]
            for metric in METRICS:
                values = same_type[metric]
                p10, p50, p90 = values.quantile([0.1, 0.5, 0.9])
                talay_value = cond[metric]
                rows.append({
                    "real_group": group,
                    "condition_id": cond.condition_id,
                    "encounter_type": cond.encounter_type,
                    "metric": metric,
                    "talay_value": round(talay_value, 2),
                    "real_p10": round(p10, 2),
                    "real_median": round(p50, 2),
                    "real_p90": round(p90, 2),
                    # share of real encounters with a value at or below Talay's
                    "talay_percentile": round(100 * (values <= talay_value).mean()),
                    "within_real_range": bool(p10 <= talay_value <= p90),
                    "real_encounters": len(values),
                })
    return pd.DataFrame(rows)


def main():
    talay = talay_conditions()
    real = real_encounters(duckdb.connect())
    result = compare(talay, real)
    talay = talay.round({"speed_ratio": 2}).drop(columns="description")
    talay.to_csv(REPORTS_DIR / "talay_scenarios.csv", index=False)
    result.to_csv(REPORTS_DIR / "talay_vs_real.csv", index=False)

    # Short view: one row per scenario family and metric, Talay's range against the real middle 80%
    view = (result.groupby(["real_group", "encounter_type", "metric"])
            .agg(talay_min=("talay_value", "min"), talay_max=("talay_value", "max"),
                 real_p10=("real_p10", "first"), real_median=("real_median", "first"),
                 real_p90=("real_p90", "first"), conditions_in_range=("within_real_range", "sum"),
                 real_encounters=("real_encounters", "first"))
            .reset_index())
    view.to_csv(REPORTS_DIR / "talay_vs_real_summary.csv", index=False)
    print(view.to_string(index=False))


if __name__ == "__main__":
    main()
