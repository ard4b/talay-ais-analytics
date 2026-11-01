"""Step 1: download a DMA daily AIS file and convert it from CSV to Parquet.

Usage:
    python -m src.common.ingest 2025-02-25 [2025-02-26 ...]

For each day this downloads aisdk-YYYY-MM-DD.zip (if missing), unpacks the CSV,
writes it as Parquet under PARQUET_DIR/date=YYYY-MM-DD/, and records how the two
formats compare in size and query time (reports/ingest_stats.csv).
"""
import csv
import sys
import time
import urllib.request
import zipfile

import duckdb

from .config import DMA_BASE_URL, PARQUET_DIR, RAW_DIR, REPORTS_DIR

# DMA column name -> our column name and DuckDB type
COLUMNS = {
    "# Timestamp": ("ts", "VARCHAR"),
    "Type of mobile": ("mobile_type", "VARCHAR"),
    "MMSI": ("mmsi", "BIGINT"),
    "Latitude": ("lat", "DOUBLE"),
    "Longitude": ("lon", "DOUBLE"),
    "Navigational status": ("nav_status", "VARCHAR"),
    "ROT": ("rot", "DOUBLE"),
    "SOG": ("sog", "DOUBLE"),
    "COG": ("cog", "DOUBLE"),
    "Heading": ("heading", "DOUBLE"),
    "IMO": ("imo", "VARCHAR"),
    "Callsign": ("callsign", "VARCHAR"),
    "Name": ("name", "VARCHAR"),
    "Ship type": ("ship_type", "VARCHAR"),
    "Cargo type": ("cargo_type", "VARCHAR"),
    "Width": ("width", "DOUBLE"),
    "Length": ("length", "DOUBLE"),
    "Type of position fixing device": ("position_device", "VARCHAR"),
    "Draught": ("draught", "DOUBLE"),
    "Destination": ("destination", "VARCHAR"),
    "ETA": ("eta", "VARCHAR"),
    "Data source type": ("data_source", "VARCHAR"),
    "A": ("dim_a", "DOUBLE"),
    "B": ("dim_b", "DOUBLE"),
    "C": ("dim_c", "DOUBLE"),
    "D": ("dim_d", "DOUBLE"),
}


def download(day):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = RAW_DIR / f"aisdk-{day}.zip"
    if not zip_path.exists():
        url = f"{DMA_BASE_URL}/{day[:4]}/aisdk-{day}.zip"
        print(f"Downloading {url}")
        urllib.request.urlretrieve(url, zip_path)
    return zip_path


def unpack(zip_path, day):
    csv_path = RAW_DIR / f"aisdk-{day}.csv"
    if not csv_path.exists():
        print(f"Unpacking {zip_path.name}")
        with zipfile.ZipFile(zip_path) as z:
            z.extract(f"aisdk-{day}.csv", RAW_DIR)
    return csv_path


def to_parquet(con, csv_path, day):
    out_dir = PARQUET_DIR / f"date={day}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "positions.parquet"

    types = {src: typ for src, (_, typ) in COLUMNS.items()}
    select = ",\n".join(f'"{src}" AS {dst}' for src, (dst, _) in COLUMNS.items() if dst != "ts")
    start = time.perf_counter()
    con.execute(f"""
        COPY (
            SELECT strptime("# Timestamp", '%d/%m/%Y %H:%M:%S') AS ts,
                   {select}
            FROM read_csv('{csv_path.as_posix()}', header = true, columns = {types})
        ) TO '{out_path.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
    """)
    print(f"Wrote {out_path} in {time.perf_counter() - start:.1f} s")
    return out_path


def timed(con, sql):
    start = time.perf_counter()
    con.execute(sql).fetchall()
    return time.perf_counter() - start


def compare_formats(con, csv_path, parquet_path, day):
    """Same question asked of both formats: average speed per ship type."""
    types = {src: typ for src, (_, typ) in COLUMNS.items()}
    csv_sql = (f"""SELECT "Ship type", avg("SOG") FROM read_csv('{csv_path.as_posix()}', header = true,
                   columns = {types}) GROUP BY 1""")
    parquet_sql = f"SELECT ship_type, avg(sog) FROM '{parquet_path.as_posix()}' GROUP BY 1"
    rows = con.execute(f"SELECT count(*) FROM '{parquet_path.as_posix()}'").fetchone()[0]
    return {
        "date": day,
        "rows": rows,
        "csv_mb": round(csv_path.stat().st_size / 1e6),
        "parquet_mb": round(parquet_path.stat().st_size / 1e6),
        "csv_query_s": round(timed(con, csv_sql), 2),
        "parquet_query_s": round(timed(con, parquet_sql), 2),
    }


def save_stats(stats):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / "ingest_stats.csv"
    existing = []
    if path.exists():
        with open(path, newline="") as f:
            existing = [r for r in csv.DictReader(f) if r["date"] != stats["date"]]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(stats))
        writer.writeheader()
        writer.writerows(existing + [stats])


def main(days):
    con = duckdb.connect()
    for day in days:
        csv_path = unpack(download(day), day)
        parquet_path = to_parquet(con, csv_path, day)
        stats = compare_formats(con, csv_path, parquet_path, day)
        save_stats(stats)
        print(stats)
        # The unpacked CSV is about 3 GB and can be unpacked again from the ZIP if needed
        csv_path.unlink()


if __name__ == "__main__":
    main(sys.argv[1:])
