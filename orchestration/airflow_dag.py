"""Production sketch: the daily pipeline as an Airflow DAG (illustration only, not run in the take-home).

The same two steps as the CLI, as separate tasks so they fail independently:
- land: wait for the provider's file, archive it write-once, register it (tiny, rarely fails);
- process: every landed feed, oldest first (checks → replace/compare days → load → Task 2).
If process fails, later runs keep landing feeds; once fixed, process catches up in order.
In production the warehouse would be Delta Lake / Iceberg on object storage instead of a DuckDB
file, so a day is still replaced atomically (replaceWhere / overwrite of the day's partition).
"""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.sensors.filesystem import FileSensor

PROVIDER_PATH = "/data/provider/feed_{{ ds }}.csv.gz"     # where the provider drops the daily file
ARCHIVE_DIR = "/data/landing"
WAREHOUSE = "/data/warehouse/aegean.duckdb"


def land_feed(ds, **_):
    from datetime import date
    from aegean import db
    from aegean.pipeline import land
    con = db.connect(WAREHOUSE)
    land(con, PROVIDER_PATH.replace("{{ ds }}", ds), date.fromisoformat(ds), ARCHIVE_DIR)


def process_feeds(**_):
    from aegean import db
    from aegean.pipeline import process
    con = db.connect(WAREHOUSE)
    outcomes = process(con)
    alerts = [r for o in outcomes for r in o[4] if not r.passed and r.severity in ("warn", "block")]
    if alerts:
        # hook for the team's alerting (Slack, e-mail, on-call); quarantined feeds need a person
        print("\n".join(f"{r.severity}: {r.name}: {r.message}" for r in alerts))


with DAG(
    dag_id="aegean_flights_daily",
    start_date=datetime(2015, 7, 1),
    schedule="0 6 * * *",          # once a day, after the provider's delivery
    catchup=True,                  # missed days are run in order once the scheduler is back
    max_active_runs=1,             # feeds must be processed in order
    default_args={"retries": 3, "retry_delay": timedelta(minutes=15), "retry_exponential_backoff": True},
) as dag:
    wait = FileSensor(task_id="wait_for_feed", filepath=PROVIDER_PATH, poke_interval=600, timeout=6 * 3600,
                      mode="reschedule")
    land_task = PythonOperator(task_id="land", python_callable=land_feed)
    process_task = PythonOperator(task_id="process", python_callable=process_feeds)
    wait >> land_task >> process_task
