"""Shared paths. AIS data is kept outside the repo (and outside OneDrive) because it is several GB."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("AIS_DATA_DIR", Path.home() / "ais_data"))
RAW_DIR = DATA_DIR / "raw"          # downloaded aisdk-YYYY-MM-DD.zip files
PARQUET_DIR = DATA_DIR / "parquet"  # one folder per day: date=YYYY-MM-DD/
CLEAN_DIR = DATA_DIR / "clean"      # cleaned positions, same layout

REPO_DIR = Path(__file__).resolve().parents[2]
REPORTS_DIR = REPO_DIR / "reports"

DMA_BASE_URL = "http://aisdata.ais.dk.s3.eu-central-1.amazonaws.com"
