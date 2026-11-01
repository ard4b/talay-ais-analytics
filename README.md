# Talay AIS Analytics

Analysis of real ship movement data (AIS) to support **Project Talay**, a ship digital twin.

- **Big Data Management (YBS405):** find real ship encounters (head-on, crossing, overtaking) and compare them with the
  test scenarios used in Talay.
- **Data Mining:** predict ship type from movement. The predictions fill in ships whose type is missing in the AIS data.

Both projects share the same data pipeline (download → Parquet → cleaning) in `src/common/`.

## Data

Historical AIS data from the **Danish Maritime Authority (DMA)**, published openly as one ZIP file per day
(`aisdk-YYYY-MM-DD.zip`, about 500–600 MB; about 3 GB as CSV).

The data is too large for Git, so it is kept outside the repository, in `~/ais_data` by default.
Set `AIS_DATA_DIR` to use another folder.

```
ais_data/
├── raw/       downloaded ZIP and CSV files
├── parquet/   date=YYYY-MM-DD/positions.parquet   (all columns, converted from CSV)
└── clean/     date=YYYY-MM-DD/positions.parquet   (cleaned ship positions)
               date=YYYY-MM-DD/vessels.parquet     (one row per ship: type, name, size)
```

## Setup

```bash
python -m venv ~/.venvs/talay-ais
~/.venvs/talay-ais/Scripts/pip install -r requirements.txt   # Windows; on macOS/Linux use bin/pip
```

## Running

```bash
python -m src.common.ingest 2025-02-25   # download, convert CSV to Parquet
python -m src.common.clean 2025-02-25    # clean and write the quality report
```

Several days can be passed at once: `python -m src.common.ingest 2025-02-24 2025-02-25`.

## Results so far (5 days: 22–26 February 2025)

**CSV vs Parquet** (`reports/ingest_stats.csv`, per day)

| | CSV | Parquet |
|---|---|---|
| File size | 2.9–3.1 GB | 388–421 MB (about 7× smaller) |
| Query time (average speed per ship type) | 3.7–5.7 s | 0.13–0.4 s (about 15–30× faster) |

**Cleaning** (`reports/cleaning_report.csv`): 84,498,127 raw rows → 42,263,871 clean ship positions from 3,877 ships.

| Step | Rows removed (5 days) |
|---|---|
| Exact duplicate rows (same message received by several stations) | 36,130,729 (42.8%) |
| Not a ship (base stations, buoys, aircraft…) | 1,974,893 |
| Invalid MMSI | 160,891 |
| Position not available or out of range | 216,147 |
| Speed or course not available | 3,591,237 |
| Speed above 50 knots | 451 |
| Same ship, same second | 159,050 |
| Position jumps | 858 |

Some ships have a track that breaks and continues far away (likely a shared MMSI or a long signal gap): 64–113 per
day, 329 different ships over the 5 days. They are kept but flagged with `suspect_track` in `vessels.parquet`.

587 of the 3,877 ships never report a usable ship type; the data mining model is meant to fill these.

**Encounters in Øresund** (`python -m src.big_data.encounters`, `reports/encounters_*.csv`)

Two moving ships (≥ 2 knots) closer than 1 nautical mile at the same minute count as an encounter. Positions are
compared only with ships in the same or a neighbouring 1 nm grid cell, so the 136,432 one-minute positions of 381
ships do not have to be compared all against all.

| | Encounters |
|---|---|
| All encounters found | 4,157 |
| Inside Copenhagen harbour (harbour buses, construction work) | 1,357 |
| Involving a service vessel (tug, pilot, dredger…) | 913 |
| At the Helsingør / Helsingborg ferry berths | 256 |
| Ships moving apart from the start | 182 |
| **Kept for analysis** | **1,449** |

| Type | Encounters | Median closest distance | Median speed | Median course difference |
|---|---|---|---|---|
| Head-on | 692 | 783 m | 10.4 kn | 176° |
| Crossing | 510 | 971 m | 10.1 kn | 115° |
| Overtaking | 247 | 754 m | 10.2 kn | 12° |

Excluded encounters are kept in the output with an `excluded_reason`, so the filtering can be checked.

**Talay scenarios vs real encounters** (`python -m src.big_data.compare_talay`, `reports/talay_vs_real*.csv`)

Talay's repository is private, so its scenario files are not included here. To run the comparison, copy
`experiments/scenarios/*.json` from the Talay repository into `talay_scenarios/`. The values used from them are in
`reports/talay_scenarios.csv`.

Talay's scenarios start ships 75–275 m apart, while real encounters start at 1 nm, so only scale-free values are
compared: the angle between the courses, the speed ratio (slower / faster ship) and the direction of the other ship
seen from the faster ship's bow. Talay places targets on a collision course on purpose, so the scenarios are also
compared with the real **close calls** (closest distance under 500 m: 88 crossing, 208 head-on, 71 overtaking).

| Scenario family | Talay | Real close calls (middle 80%, median) | Finding |
|---|---|---|---|
| Crossing: course angle | 90° | 90–163°, median 124° | Real crossings are more often oblique (closer to head-on) than square |
| Crossing: speed ratio | 0.50–0.75 | 0.49–0.96, median 0.82 | Realistic, but 4 of 5 conditions use the low end (0.50) |
| Head-on: speed ratio | 0.50–0.83 | 0.60–0.97, median 0.82 | Only head_on-c2 (0.83) is typical; 0.50 is rarer than 1 in 10 |
| Head-on: other ship's direction | 0–2° | 4–19°, median 10° | Talay is a dead-on collision course; real close calls pass slightly offset |
| Overtaking: speed ratio | 0.33–0.50 | 0.44–0.90, median 0.69 | Talay overtakes much faster than real ships do |
| Head-on / overtaking: course angle | exactly 180° / 0° | 169–180° / 1–44° | Talay's ±5° heading jitter covers most of the real spread |

Suggested additions to Talay's scenarios: an oblique crossing (about 120°), more conditions with a speed ratio around
0.8, a head-on condition with the target about 10° off the bow, and an overtaking condition with a speed ratio
around 0.7.

**Ship type from movement (Data Mining)** (`python -m src.data_mining.features`, then `python -m src.data_mining.train`)

Each ship's track is cut into one-hour windows (a gap over 30 minutes starts a new segment; at least 20 minutes of
positions per window), and each window is described by 22 movement features: speed, acceleration, turning, heading
vs course, stops, distance and how straight the path is. Ship type is only the label. Name, size, MMSI and position
are never used as inputs. Windows where the ship does not move at all (moored or anchored) are left out.

Classes: Cargo, Tanker, Passenger and Fishing. That gives 1,639 ships and 46,071 moving windows. The models are
scored on 328 ships they never saw during training (`reports/data_mining/`):

| Model | Cross-validation macro-F1 (training ships) | Test macro-F1, per ship | Test accuracy, per ship |
|---|---|---|---|
| Dummy (most frequent) | 0.15 | 0.17 | 0.49 |
| Dummy (stratified) | 0.25 | 0.22 | 0.43 |
| Logistic regression | 0.65 | 0.67 | 0.65 |
| Decision tree | 0.66 | 0.68 | 0.67 |
| **Random forest** | **0.70** | **0.74** | **0.75** |

- Fishing (F1 0.91), Cargo (0.78) and Passenger (0.77) are recognised well. Tanker (0.50) is mostly confused with
  Cargo: both sail straight at about 10–12 knots, so their movement is very similar.
- The most useful features are maximum, minimum and mean speed, and how fast the heading changes.
- Splitting by row instead of by ship lets windows of the same ship end up in both training and test data. That
  raises macro-F1 from 0.70 to 0.74, a result the model did not really earn.
- The random forest, refitted on all labelled ships, predicts a type for the 161 unknown-type ships that move.
  Only 51 get a probability of 0.6 or more. Many unknown ships are probably none of the four classes (small leisure
  or work boats), so the predictions are only used when the model is confident. In the dashboard they appear as
  e.g. "Cargo (predicted)".

**Power BI dashboard** (`python -m src.big_data.powerbi_export`)

All tables for the dashboard are written to one Excel workbook, `reports/powerbi/talay_ais_dashboard.xlsx`, one
sheet per table (summary numbers, cleaning steps, CSV vs Parquet, traffic per hour and per ~1 km cell, encounters,
and the Talay comparison). Excel keeps the column types, so Power BI does not misread decimals or dates. The
step-by-step dashboard guide (in Turkish) is in [docs/powerbi_rehberi.md](docs/powerbi_rehberi.md).

## Repository layout

```
src/common/       shared pipeline: config, ingest, clean
src/big_data/     encounter detection, Talay comparison, Power BI tables
src/data_mining/  movement features and ship type classification
reports/          generated statistics and quality reports; reports/powerbi/ holds the dashboard data
docs/             Power BI guide
```
