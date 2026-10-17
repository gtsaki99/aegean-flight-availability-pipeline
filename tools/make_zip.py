"""Build the submission zip next to the project folder: ../Georgios_Tsakiridis_assignment.zip

Includes code, tests, lookups, outputs and the write-up; excludes the Kaggle files and everything
generated at run time (warehouse, demo working files, caches).
    python tools/make_zip.py
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "Georgios_Tsakiridis_assignment"   # name of the zip and of its top folder
TARGET = ROOT.parent / f"{PACKAGE}.zip"
INCLUDE = ["README.md", "WRITEUP.pdf", "requirements.txt", "pytest.ini", "aegean", "analysis", "orchestration",
           "tests", "tools", "docs", "data/lookups", "data/raw/PUT_KAGGLE_FILES_HERE.txt", "output"]
EXCLUDE_PARTS = {"__pycache__", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".duckdb", ".wal", ".html"}


def files():
    for entry in INCLUDE:
        path = ROOT / entry
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for p in candidates:
            if EXCLUDE_PARTS & set(p.parts) or p.suffix in EXCLUDE_SUFFIXES:
                continue
            yield p


def main():
    with zipfile.ZipFile(TARGET, "w", zipfile.ZIP_DEFLATED) as archive:
        count = 0
        for p in files():
            archive.write(p, Path(PACKAGE) / p.relative_to(ROOT))
            count += 1
    print(f"written: {TARGET} ({count} files, {TARGET.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
