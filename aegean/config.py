"""Project paths. The Kaggle files are read from RAW_DIR, which can be overridden
with the AEGEAN_RAW_DIR environment variable or the --raw-dir option."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = Path(os.environ.get("AEGEAN_RAW_DIR", ROOT / "data" / "raw"))
LOOKUP_DIR = ROOT / "data" / "lookups"
BTS_DIR = LOOKUP_DIR / "bts"
OUTPUT_DIR = ROOT / "output"
WAREHOUSE_DIR = ROOT / "warehouse"


def raw_file(name: str) -> Path:
    path = RAW_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Download the Kaggle 'flight-delays' files into {RAW_DIR} "
            "or point AEGEAN_RAW_DIR / --raw-dir at the folder that contains them."
        )
    return path
