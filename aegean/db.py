"""Warehouse connection: a DuckDB database file with the lookups attached as tables.

Locally the flights "partitions" are the flight_date values of one DuckDB table: a day is
replaced as a unit inside one transaction (see load.py).
"""
import duckdb

from . import config

MACROS = [
    # 4-digit HHMM → minutes after midnight ('2400' → 1440); NULL if malformed
    "create or replace macro hhmm_min(x) as try_cast(substr(x, 1, 2) as int) * 60 + try_cast(substr(x, 3, 2) as int)",
    # a minute-of-day difference wrapped into (-720, 720]
    "create or replace macro wrap(x) as ((x) % 1440 + 1440 + 720) % 1440 - 720",
    "create or replace macro local_min(ts) as hour(ts) * 60 + minute(ts)",
]

LOOKUPS = {
    "airports": "airports.csv",
    "airlines": "airlines.csv",
    "airline_groups": "airline_groups.csv",
    "holidays": "holidays.csv",
    "cancellation_reasons": "cancellation_reasons.csv",
}


def connect(path=None):
    """Open the warehouse (default warehouse/aegean.duckdb; ':memory:' for tests)."""
    if path is None:
        config.WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
        path = config.WAREHOUSE_DIR / "aegean.duckdb"
    con = duckdb.connect(str(path))
    con.sql("SET TimeZone = 'UTC'")
    con.sql("SET enable_progress_bar = false")
    for macro in MACROS:
        con.sql(macro)
    for table, file in LOOKUPS.items():
        con.sql(f"create or replace temp table {table} as select * from "
                f"read_csv('{(config.LOOKUP_DIR / file).as_posix()}', all_varchar = true)")
    return con
