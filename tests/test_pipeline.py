"""Pipeline scenarios at small scale: a made-up schedule (3 aircraft, 12 flights a day) is the truth,
the simulator sends heavily corrected feeds, and the pipeline must end up equal to the truth."""
from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from aegean import availability, db
from aegean.load import DIGEST_SQL, initial_load
from aegean.pipeline import land, process, rebuild, set_watermark, watermark
from aegean.schema import SOURCE_NAMES
from aegean.simulator import Simulator
from aegean.stage import load_raw_csv, stage
from factory import make_flight

FIRST_DAY, INITIAL_UNTIL, LAST_DAY = date(2015, 6, 1), date(2015, 6, 30), date(2015, 7, 20)
HEAVY = {"late": 0.3, "tail": 0.2, "delay": 0.3, "diverted": 1.0, "phantom": 0.1}


def schedule():
    rows = []
    for n in range((LAST_DAY - FIRST_DAY).days + 1):
        day = (FIRST_DAY + timedelta(days=n)).isoformat()
        for a in range(3):
            for i, hour in enumerate(("0600", "1000", "1400", "1800")):
                origin, dest = ("ORD", "MSP") if i % 2 == 0 else ("MSP", "ORD")
                status = "cancelled" if (n + a + i) % 37 == 0 else "diverted" if (n + a + i) % 41 == 0 else "normal"
                rows.append(make_flight(day=day, airline="AA", number=100 * a + i, tail=f"N{a}AA", origin=origin,
                                        dest=dest, sched_dep=hour, delay=(n * 7 + a * 3 + i) % 40, taxi_out=10,
                                        air=100, taxi_in=10, sched_time=120, status=status,
                                        diverted_total=300 if status == "diverted" else None))
    return rows


@pytest.fixture(scope="module")
def truth_csv(tmp_path_factory):
    path = tmp_path_factory.mktemp("truth") / "flights.csv"
    pd.DataFrame(schedule(), columns=SOURCE_NAMES).to_csv(path, index=False)
    return path


@pytest.fixture
def env(tmp_path, truth_csv):
    con = db.connect(":memory:")
    initial_load(con, truth_csv, until=INITIAL_UNTIL.isoformat())
    set_watermark(con, INITIAL_UNTIL)
    sim = Simulator(truth_csv, tmp_path / "provider", date(2015, 7, 1), LAST_DAY, rates=HEAVY)
    return con, sim, tmp_path / "landing", truth_csv


def daily(con, sim, archive, day, process_now=True):
    feed_id, status = land(con, sim.daily_feed(day), day, archive)
    return process(con, compute_availability=False) if process_now else status


def results_of(outcomes):
    return {r.name.split(":")[0]: r for o in outcomes for r in o[4]}


def matches_truth(con, truth_csv, first, last):
    load_raw_csv(con, truth_csv, table="raw_truth",
                 where=f"make_date(cast(YEAR as int), cast(MONTH as int), cast(DAY as int)) between date '{first}' and date '{last}'")
    stage(con, feed_id=-1, raw="raw_truth", out="truth")
    return con.sql(f"""select bool_and(t.digest = d.digest) from (select flight_date, {DIGEST_SQL} from truth group by 1) t
                       left join day_digest d using (flight_date)""").fetchone()[0]


def test_replaying_corrected_feeds_converges_to_the_truth(env):
    con, sim, archive, truth = env
    for n in range(15):
        daily(con, sim, archive, date(2015, 7, 1) + timedelta(days=n))
    changes = con.sql("select sum(corrected), sum(added), sum(removed) from change_log where feed_id > 0").fetchone()
    assert all(c > 0 for c in changes)                       # corrections, additions and removals happened
    assert matches_truth(con, truth, date(2015, 6, 21), date(2015, 7, 15) - timedelta(days=6))
    assert not matches_truth(con, truth, date(2015, 7, 13), date(2015, 7, 15))   # still inside the window


def test_duplicate_and_old_feeds_are_not_applied(env):
    con, sim, archive, _ = env
    path = sim.daily_feed(date(2015, 7, 1))
    assert land(con, path, date(2015, 7, 1), archive)[1] == "LANDED"
    assert land(con, path, date(2015, 7, 1), archive)[1] == "DUPLICATE"
    process(con, compute_availability=False)
    daily(con, sim, archive, date(2015, 7, 2))
    old = sim.daily_feed(date(2015, 7, 1), name="resent.csv.gz", reorder=True)
    land(con, old, date(2015, 7, 1), archive)
    assert process(con, compute_availability=False)[0][3] == "SKIPPED_OLD"
    assert watermark(con) == date(2015, 7, 2)


def test_two_missed_feeds_lose_nothing(env):
    con, sim, archive, _ = env
    daily(con, sim, archive, date(2015, 7, 1))
    outcomes = daily(con, sim, archive, date(2015, 7, 4))    # 2 and 3 missed
    assert "coverage_gap" not in results_of(outcomes)


def test_five_missed_feeds_create_a_gap_that_a_backfill_fixes(env):
    con, sim, archive, truth = env
    daily(con, sim, archive, date(2015, 7, 1))
    outcomes = daily(con, sim, archive, date(2015, 7, 7))    # 2..6 missed
    gap = results_of(outcomes)["coverage_gap"]
    assert "2015-06-26..2015-06-27" in gap.message        # W-5 .. t-n with W = Jul 1, t = Jul 7
    assert con.sql("select count(*) from dq_status").fetchone()[0] == 2
    land(con, sim.extract(date(2015, 6, 26), date(2015, 6, 27)), date(2015, 7, 7), archive, "backfill",
         date(2015, 6, 26), date(2015, 6, 27))
    assert process(con, compute_availability=False)[0][3] == "PROCESSED"
    assert con.sql("select count(*) from dq_status").fetchone()[0] == 0
    assert matches_truth(con, truth, date(2015, 6, 26), date(2015, 6, 27))


def test_bad_feeds_are_quarantined_and_the_next_one_covers_them(env):
    con, sim, archive, truth = env
    daily(con, sim, archive, date(2015, 7, 1))
    land(con, sim.truncated_feed(date(2015, 7, 2)), date(2015, 7, 2), archive)
    assert process(con, compute_availability=False)[0][3] == "QUARANTINED"
    land(con, sim.schema_changed_feed(date(2015, 7, 3)), date(2015, 7, 3), archive)
    assert process(con, compute_availability=False)[0][3] == "QUARANTINED"
    assert watermark(con) == date(2015, 7, 1)
    for n in range(4, 11):
        daily(con, sim, archive, date(2015, 7, n))
    assert matches_truth(con, truth, date(2015, 6, 21), date(2015, 7, 4))


def test_changed_final_day_is_detected_and_not_written(env):
    con, sim, archive, _ = env
    daily(con, sim, archive, date(2015, 7, 1))
    before = con.sql("select digest from day_digest where flight_date = '2015-06-23'").fetchone()[0]
    land(con, sim.history_changed_feed(date(2015, 7, 2), date(2015, 6, 23)), date(2015, 7, 2), archive)
    recon = results_of(process(con, compute_availability=False))["reconciliation"]
    assert not recon.passed and "2015-06-23" in recon.message
    assert con.sql("select digest from day_digest where flight_date = '2015-06-23'").fetchone()[0] == before


def test_processing_outage_catches_up_in_order(env):
    con, sim, archive, _ = env
    for n in (3, 1, 2):                                       # landed out of order
        daily(con, sim, archive, date(2015, 7, n), process_now=False)
    outcomes = process(con, compute_availability=False)
    assert [(o[2], o[3]) for o in outcomes] == [(date(2015, 7, n), "PROCESSED") for n in (1, 2, 3)]
    assert watermark(con) == date(2015, 7, 3)


def test_rebuild_from_the_archive_gives_the_same_result(env, tmp_path):
    con, sim, archive, truth = env
    for n in range(1, 9):
        if n != 4:                                            # include a missed day
            daily(con, sim, archive, date(2015, 7, n))
    availability.compute(con)
    db_file = tmp_path / "source.duckdb"
    con.sql(f"attach '{db_file.as_posix()}' as src")
    for table in ("feed_manifest", "flights_availability", "day_digest"):
        con.sql(f"create table src.{table} as select * from {table}")
    con.sql("detach src")
    source = duckdb.connect(str(db_file), read_only=True)
    rebuilt = db.connect(":memory:")
    rebuild(rebuilt, source, truth, INITIAL_UNTIL.isoformat())
    rebuilt.sql(f"attach '{db_file.as_posix()}' as src (read_only)")
    assert rebuilt.sql("""select count(*) from (select flight_date, row_count, digest from day_digest
                          except select flight_date, row_count, digest from src.day_digest)""").fetchone()[0] == 0
    assert rebuilt.sql("""select count(*) from (select * from flights_availability
                          except select * from src.flights_availability)""").fetchone()[0] == 0
