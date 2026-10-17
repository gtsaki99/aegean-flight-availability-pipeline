"""Task 2 cases with hand-computed expected minutes.

ORD and MSP are both on Chicago time, so most cases avoid timezone arithmetic; the timezone cases
use JFK (New York) and LAX (Los Angeles).
"""
from datetime import date

import pytest

from aegean import availability, db
from aegean.load import apply_days, ensure_tables
from factory import make_flight, stage_rows

D = "2015-01-13"


@pytest.fixture
def con():
    return db.connect(":memory:")


def run(con, rows):
    stage_rows(con, rows)
    ensure_tables(con)
    days = [r[0] for r in con.sql("select distinct flight_date from stage").fetchall()]
    apply_days(con, 1, days)
    availability.compute(con)
    return con.sql("select * from flights_availability").df()


def row(result, day=D, airline="AA"):
    match = result[(result.flight_date.astype(str) == day) & (result.airline == airline)]
    assert len(match) == 1, f"expected one row for {airline} on {day}:\n{result}"
    return match.iloc[0]


def chicago(sched, minutes, **kw):
    """ORD → MSP (or back) flight of `minutes` gate to gate: 10 taxi out + air + 10 taxi in."""
    return make_flight(origin=kw.pop("origin", "ORD"), dest=kw.pop("dest", "MSP"), sched_dep=sched, delay=0,
                       taxi_out=10, air=minutes - 20, taxi_in=10, sched_time=minutes, **kw)


def test_simple_day(con):
    r = row(run(con, [chicago("0730", 120), chicago("1100", 180, number=2, origin="MSP", dest="ORD")]))
    assert (r.availability, r.night_availability) == (24 * 60 - 300, 8 * 60)
    assert (r.n_aircraft, r.idle_day_availability) == (1, 0)


def test_flight_crossing_midnight_is_split_across_days(con):
    # N1 flies 23:00 → 01:00. N2 flies on the 14th (10:00–12:00), so the 14th is part of the data:
    # the output never goes beyond the last flight day loaded.
    result = run(con, [chicago("2300", 120), chicago("1000", 120, day="2015-01-14", tail="N2", number=2)])
    first, second = row(result), row(result, "2015-01-14")
    assert (first.availability, first.night_availability) == (23 * 60, 7 * 60)
    assert (second.availability, second.night_availability) == (23 * 60 + 22 * 60, (6 * 60 + 60) + 8 * 60)
    assert second.n_aircraft == 2


def test_output_stops_at_the_last_flight_day(con):
    result = run(con, [chicago("2300", 120)])        # lands at 01:00 on the 14th, a day not yet in the data
    assert list(result.flight_date.astype(str)) == [D]


def test_timezone_crossing_uses_each_airports_local_day(con):
    # JFK 06:27 EST → LAX 09:51 PST: 6:27 on the ground in New York + 14:09 in Los Angeles
    r = row(run(con, [make_flight()]))
    assert r.availability == 387 + 849 and r.night_availability == 387 + 60


@pytest.mark.parametrize("idle_day, minutes, night", [
    ("2015-03-08", 23 * 60, 7 * 60),     # spring forward: 23 h day, 7 h night
    ("2015-11-01", 25 * 60, 9 * 60),     # fall back: 25 h day, 9 h night
    ("2015-01-13", 24 * 60, 8 * 60),
])
def test_idle_day_between_flights_counts_as_a_full_day(con, idle_day, minutes, night):
    before, after = date.fromordinal(date.fromisoformat(idle_day).toordinal() - 1), \
        date.fromordinal(date.fromisoformat(idle_day).toordinal() + 1)
    rows = [make_flight(day=before.isoformat(), origin="JFK", dest="BOS", sched_dep="1000", air=40, sched_time=70),
            make_flight(day=after.isoformat(), number=2, origin="BOS", dest="JFK", sched_dep="1000", air=40, sched_time=70)]
    r = row(run(con, rows), idle_day)
    assert (r.availability, r.night_availability) == (minutes, night)
    assert (r.n_aircraft, r.idle_day_availability) == (1, 1)


def test_day_with_only_a_cancelled_flight_is_a_full_idle_day(con):
    rows = [chicago("1000", 120, day="2015-01-12"),
            chicago("1000", 120, day="2015-01-13", number=2, origin="MSP", dest="ORD", status="cancelled"),
            chicago("1000", 120, day="2015-01-14", number=3, origin="MSP", dest="ORD")]
    r = row(run(con, rows))
    assert (r.availability, r.idle_day_availability, r.n_cancelled) == (24 * 60, 1, 1)


def test_idle_days_across_the_us_to_aa_switch(con):
    rows = [chicago("1000", 120, day="2015-06-29", airline="US", tail="N1"),
            chicago("1000", 120, day="2015-07-03", airline="AA", tail="N1", number=2, origin="MSP", dest="ORD"),
            # other aircraft that show which codes report flights on which days
            chicago("1000", 120, day="2015-06-30", airline="US", tail="N2", number=3),
            chicago("1000", 120, day="2015-07-01", airline="AA", tail="N3", number=4),
            chicago("1000", 120, day="2015-07-02", airline="AA", tail="N3", number=5, origin="MSP", dest="ORD")]
    result = run(con, rows)
    assert row(result, "2015-06-30", "US").idle_day_availability == 1          # N1 still counts for US
    assert row(result, "2015-07-01", "AA").idle_day_availability == 1          # US no longer reports: AA
    assert row(result, "2015-07-02", "AA").idle_day_availability == 1
    assert not ((result.airline == "US") & (result.flight_date.astype(str) >= "2015-07-01")).any()


def test_diverted_flight_with_arrival(con):
    # JFK 06:27 EST → LAX after 600 min (13:27 PST): 6:27 in New York + 10:33 in Los Angeles
    r = row(run(con, [make_flight(status="diverted", diverted_total=600)]))
    assert r.availability == 387 + 633 and r.n_diverted == 1 and r.estimated_minutes == 0


def test_diverted_flight_without_arrival_is_estimated_and_capped(con):
    # scheduled 120 min from 08:00, but the aircraft departs again at 09:00: flying 08:00–09:00 (estimated)
    rows = [chicago("0800", 120, status="diverted"), chicago("0900", 120, number=2)]
    r = row(run(con, rows))
    assert r.availability == 8 * 60 + 13 * 60 and r.estimated_minutes == 60


def test_cancelled_after_take_off_is_estimated(con):
    cancelled = chicago("0800", 120, status="cancelled", DEPARTURE_TIME="0800", DEPARTURE_DELAY="0",
                        TAXI_OUT="10", WHEELS_OFF="0810")
    r = row(run(con, [cancelled]))
    assert r.availability == 24 * 60 - 120 and r.estimated_minutes == 120 and r.n_cancelled == 1


def test_overlapping_flights_are_merged(con):
    rows = [chicago("1000", 180), chicago("1200", 120, number=2, origin="MSP", dest="ORD")]   # 10–13 and 12–14
    result = run(con, rows)
    assert row(result).availability == 24 * 60 - 240
    assert con.sql("select sum(n_overlaps) from timeline_metrics").fetchone()[0] == 1


def test_ground_time_is_placed_at_the_next_departure_airport(con):
    # lands at LAX 09:51 PST (17:51 UTC), next departs from ORD: the wait is on Chicago time
    rows = [make_flight(), make_flight(day="2015-01-14", number=2, origin="ORD", dest="MSP", sched_dep="1200",
                                       delay=0, air=100, sched_time=120)]
    r = row(run(con, rows))
    assert r.availability == 387 + (24 * 60 - (11 * 60 + 51))    # Chicago: 11:51 → 24:00
    assert con.sql("select sum(n_chain_breaks) from timeline_metrics").fetchone()[0] == 1


def test_night_window_boundaries(con):
    r = row(run(con, [chicago("0500", 120)]))                    # flying exactly 05:00–07:00
    assert r.night_availability == 5 * 60 + 60 and r.availability == 24 * 60 - 120


def test_airline_group_view_combines_merged_codes(con):
    rows = [chicago("1000", 120, day=D, airline="US", tail="N1"), chicago("1000", 120, day=D, airline="AA", tail="N2", number=2),
            chicago("1000", 120, day=D, airline="DL", tail="N3", number=3)]
    run(con, rows)
    groups = dict(con.sql("select airline_group, availability from flights_availability_by_group").fetchall())
    assert groups == {"AA_GROUP": 2 * (24 * 60 - 120), "DL": 24 * 60 - 120}


def test_output_invariants(con):
    rows = [chicago("0730", 120), chicago("2300", 120, number=2, origin="MSP", dest="ORD"),
            chicago("1000", 120, day="2015-01-16", number=3)]
    result = run(con, rows)
    assert (result.night_availability <= result.availability).all()
    assert (result.availability <= result.n_aircraft * 25 * 60).all()
    assert (result.idle_day_availability <= result.n_aircraft).all()
    assert list(result.flight_date.astype(str)) == ["2015-01-13", "2015-01-14", "2015-01-15", "2015-01-16"]
