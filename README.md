# Aegean data engineering exercise: daily flights pipeline and aircraft availability

Georgios Tsakiridis · 2015 US flight data (BTS on-time data, Kaggle "flight-delays").

- **Task 1:** store `flights` and update it daily from a rolling feed of the last n flight days, where the 7 most recent days may still be corrected.
- **Task 2:** `flights_availability`: ground time per airline and day, with the part between 23:00 and 07:00.

**Start with `WRITEUP.pdf`** (also `docs/WRITEUP.md`): the execution plan, the decisions and the reasoning behind them, the results and the limitations.

## What's in the package

| Path | Contents |
|---|---|
| `WRITEUP.pdf`, `docs/WRITEUP.md` | The write-up (the profiling report is its appendix) |
| `docs/architecture.svg` | Pipeline diagram |
| `aegean/` | The pipeline (Python package, run as `python -m aegean <command>`) |
| `analysis/profiling.py` | Recomputes every data finding quoted in the write-up |
| `orchestration/airflow_dag.py` | Production sketch as an Airflow DAG (illustration, not run) |
| `tests/` | 64 tests (pytest): hand-computed Task 2 cases, stage, checks, load and pipeline scenarios |
| `data/lookups/` | Lookup tables (airports with timezones and BTS IDs, airlines, airline groups, holidays, cancellation reasons) and the cached BTS lookups |
| `data/raw/` | Put the Kaggle files here (not included, see below) |
| `output/` | Results: `flights_availability.csv/.parquet`, `flights_availability_by_group.csv/.parquet`, the AA/US chart, and the reports (lookups, initial-load data quality, sanity, profiling, demo) |

## Setup

Requires Python 3.10+ (developed and tested with 3.13 on Windows 11).

```
pip install -r requirements.txt
```

The Kaggle files aren't included: flights.csv is 590 MB, and still ~195 MB compressed, well above typical email attachment limits (20–25 MB). Download `flights.csv`, `airports.csv` and `airlines.csv` from
https://www.kaggle.com/usdot/flight-delays and put them in `data/raw/`. To use another folder, pass `--raw-dir <folder>`
before the command (e.g. `python -m aegean --raw-dir C:\data build-lookups`) or set `AEGEAN_RAW_DIR`.

## Run

From this folder, in this order:

| Command | What it does | Time* |
|---|---|---|
| `python -m aegean build-lookups` | Rebuilds `data/lookups/airports.csv` (timezones, October's BTS airport IDs) and `airlines.csv`, with validation → `output/lookups_report.md`. Uses the cached BTS files (no internet needed; `--refresh-bts` downloads them again). | ~10 s |
| `python -m aegean full-run` | Loads the whole of 2015 into the warehouse (`warehouse/aegean.duckdb`) with all checks → `output/initial_load_dq_report.md`, then computes Task 2 → `output/flights_availability.*` | ~1.5 min |
| `python -m aegean report` | Task 2 sanity report and the AA / US / AA_GROUP chart → `output/sanity_report.md`, `output/aa_us_availability.png` | ~10 s |
| `python -m aegean profile` | The analysis findings, recomputed → `output/profiling_report.md` (reads the size of flights.csv for the feed-size table, so pass `--raw-dir` if the Kaggle files aren't in `data/raw/`) | ~10 s |
| `python -m aegean demo` | The daily pipeline end to end: initial load up to 2015-06-30, then 35 daily feeds from a simulated provider with 11 failure scenarios, verified automatically, plus a rebuild from the archive → `output/demo_report.md` | ~10 min |
| `python -m pytest` | All tests (no Kaggle files needed) | ~30 s |

\* On a recent laptop. The demo writes ~0.9 GB of working files to `demo/`, which can be deleted afterwards.

Daily operation (what the demo does each day): `python -m aegean land <file> --as-of YYYY-MM-DD` (use `--kind backfill --range-from … --range-to …` for a one-off extract), then `python -m aegean process`. `python -m aegean rebuild --source <db> --target <db> --until YYYY-MM-DD` rebuilds a warehouse from the initial load plus every archived feed.

## Output: `flights_availability`

One row per airline and local day (minutes unless noted):

| Column | Meaning |
|---|---|
| `flight_date`, `airline` | Local day and airline code (as reported) |
| `n_aircraft` | Aircraft counted that day: every aircraft between its first and last recorded flight |
| `availability` | Total ground time (time not flying) of those aircraft |
| `night_availability` | The part of it between 23:00 and 07:00 local |
| `idle_day_availability` | Number of aircraft on the ground the whole day (a precursor signal, not a cause) |
| `n_cancelled`, `n_diverted` | Flights of that day, for context (e.g. storm days) |
| `estimated_minutes` | Flying time that had to be estimated (diverted without an arrival, cancelled after take-off) |
| `is_provisional` | The day is inside the 7-day correction window of the latest feed |

`flights_availability_by_group` has the same columns per airline group (e.g. `AA_GROUP` = AA + US, comparable across the July 2015 merger).
