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

## Repository layout

```
src/common/       shared pipeline: config, ingest, clean
src/big_data/     encounter detection; Power BI tables and Talay comparison (next)
src/data_mining/  ship type classification (next)
talay_scenarios/  copies of Talay's test scenarios, used for the comparison
reports/          generated statistics and quality reports
```
