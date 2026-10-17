from datetime import date, timedelta

import pytest

from aegean import db
from aegean.checks import feed_checks, history_checks
from aegean.load import apply_days, ensure_tables, reconcile
from factory import make_flight, stage_rows

START = date(2015, 5, 4)   # a Monday; no holidays in the 5 weeks that follow


@pytest.fixture
def con():
    return db.connect(":memory:")


def day(n):
    return (START + timedelta(days=n)).isoformat()


def schedule(n, airline, flights, tail_prefix):
    """`flights` flights of one airline on day n, each with its own aircraft."""
    return [make_flight(day=day(n), airline=airline, number=i, tail=f"{tail_prefix}{i:03d}",
                        sched_dep=f"{6 + i % 12:02d}00") for i in range(flights)]


def load_days(con, rows, days, feed_id=1):
    stage_rows(con, rows, feed_id=feed_id)
    ensure_tables(con)
    return apply_days(con, feed_id, days)


def by_name(results):
    return {r.name: r for r in results}


# --- feed checks ------------------------------------------------------------------------------

def test_clean_feed_passes(con):
    stage_rows(con, [make_flight(number=i) for i in range(3)])
    results = feed_checks(con, expected_days=[date(2015, 1, 13)])
    assert not [r for r in results if not r.passed]


def test_missing_day_blocks(con):
    stage_rows(con, [make_flight(day="2015-01-13")])
    r = by_name(feed_checks(con, expected_days=[date(2015, 1, 12), date(2015, 1, 13)]))["coverage"]
    assert r.blocking and "2015-01-12" in r.message


def test_duplicates_block(con):
    stage_rows(con, [make_flight(), make_flight()])
    results = by_name(feed_checks(con))
    assert results["unique_key"].blocking and results["exact_duplicates"].blocking


def test_strict_rule_blocks_only_above_threshold(con):
    rows = [make_flight(number=i) for i in range(20_000)] + [make_flight(number=99_999, ELAPSED_TIME="400")]
    stage_rows(con, rows)
    assert not by_name(feed_checks(con))["rule_elapsed_parts"].blocking      # 1 in 20,001 < 0.01%
    stage_rows(con, [make_flight(number=1), make_flight(number=2, ELAPSED_TIME="400")])
    assert by_name(feed_checks(con))["rule_elapsed_parts"].blocking          # 1 in 2


def test_unknown_airline_blocks_on_first_occurrence(con):
    rows = [make_flight(number=i) for i in range(20_000)] + [make_flight(airline="ZZ")]
    stage_rows(con, rows)
    assert by_name(feed_checks(con))["unknown_airline"].blocking


# --- history checks ---------------------------------------------------------------------------

def test_merger_is_detected_by_disappearance_and_aircraft_moves(con):
    history = []
    for n in range(28):
        history += schedule(n, "AA", 60, "NA") + schedule(n, "US", 60, "NU") + schedule(n, "DL", 60, "ND")
    load_days(con, history, [date.fromisoformat(day(n)) for n in range(28)])

    # day 28: US is gone, its aircraft fly as AA; AA_GROUP volume is unchanged
    merged = schedule(28, "AA", 60, "NA") + schedule(28, "DL", 60, "ND")
    merged += [dict(f, AIRLINE="AA", FLIGHT_NUMBER=str(1000 + i)) for i, f in enumerate(schedule(28, "US", 60, "NU"))]
    stage_rows(con, merged, feed_id=2)
    results = by_name(history_checks(con, [date.fromisoformat(day(28))]))
    assert f"airline_disappeared:{day(28)}:US" in results
    move = results[f"tail_airline_change:{day(28)}:US->AA"]
    assert move.severity == "warn" and move.value == 60
    assert not [n for n in results if n.startswith("airline_volume")]     # group level: no alert


def test_volume_drop_alerts_but_not_on_a_holiday(con):
    history = []
    for n in range(28):
        history += schedule(n, "DL", 60, "ND")
    load_days(con, history, [date.fromisoformat(day(n)) for n in range(28)])
    stage_rows(con, schedule(28, "DL", 30, "ND"), feed_id=2)
    results = by_name(history_checks(con, [date.fromisoformat(day(28))]))
    assert results[f"day_volume:{day(28)}"].severity == "warn"
    assert results[f"airline_volume:{day(28)}:DL"].severity == "warn"

    # the same drop on Memorial Day (2015-05-25 = day 21) is only information
    stage_rows(con, schedule(21, "DL", 30, "ND"), feed_id=3)
    results = by_name(history_checks(con, [date(2015, 5, 25)]))
    assert results["day_volume:2015-05-25"].severity == "info"


# --- load -------------------------------------------------------------------------------------

def test_day_replacement_logs_corrections_additions_and_removals(con):
    d = date(2015, 1, 13)
    first = [make_flight(number=1), make_flight(number=2), make_flight(number=3)]
    load_days(con, first, [d], feed_id=1)
    digest_1 = con.sql("select digest from day_digest").fetchone()[0]

    second = [make_flight(number=1, delay=45), make_flight(number=3), make_flight(number=4)]
    load_days(con, second, [d], feed_id=2)
    log = con.sql("select corrected, added, removed, stored_rows from change_log where feed_id = 2").fetchone()
    assert log == (1, 1, 1, 3)
    stored = con.sql("select FLIGHT_NUMBER, DEPARTURE_DELAY from flights order by 1").fetchall()
    assert stored == [(1, 45), (3, 27), (4, 27)]
    digest_2 = con.sql("select digest from day_digest").fetchone()[0]
    assert digest_2 != digest_1

    # idempotent: the same feed again changes nothing
    load_days(con, second, [d], feed_id=3)
    assert con.sql("select corrected, added, removed from change_log where feed_id = 3").fetchone() == (0, 0, 0)
    assert con.sql("select digest from day_digest").fetchone()[0] == digest_2
    assert con.sql("select count(*) from flights").fetchone()[0] == 3


def test_only_the_replaced_days_are_touched(con):
    rows = [make_flight(day="2015-01-12"), make_flight(day="2015-01-13")]
    load_days(con, rows, [date(2015, 1, 12), date(2015, 1, 13)])
    load_days(con, [make_flight(day="2015-01-13", delay=5)], [date(2015, 1, 13)], feed_id=2)
    assert con.sql("select flight_date, DEPARTURE_DELAY from flights order by 1").fetchall() == [
        (date(2015, 1, 12), 27), (date(2015, 1, 13), 5)]


def test_reconciliation_detects_a_changed_final_day(con):
    d = date(2015, 1, 13)
    load_days(con, [make_flight(number=1), make_flight(number=2)], [d])
    stage_rows(con, [make_flight(number=1), make_flight(number=2)], feed_id=2)
    assert reconcile(con, [d])[0].passed
    stage_rows(con, [make_flight(number=1), make_flight(number=2, delay=90)], feed_id=3)
    r = reconcile(con, [d])[0]
    assert not r.passed and "2015-01-13" in r.message
    assert con.sql("select DEPARTURE_DELAY from flights where FLIGHT_NUMBER = 2").fetchone()[0] == 27   # not written


def test_large_share_of_changes_raises_an_alert(con):
    d = date(2015, 1, 13)
    load_days(con, [make_flight(number=i) for i in range(10)], [d])
    results = load_days(con, [make_flight(number=i, delay=60) for i in range(10)], [d], feed_id=2)
    assert not by_name(results)["change_share"].passed
