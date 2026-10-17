from datetime import date, datetime

import pandas as pd
import pytest

from aegean import db, tz
from aegean.stage import FeedFormatError, load_raw_csv
from factory import bts_id, make_flight, stage_rows


def ts(text):
    return pd.Timestamp(text, tz="UTC")


@pytest.fixture
def con():
    return db.connect(":memory:")


def one(con, **kwargs):
    return stage_rows(con, [make_flight(**kwargs)]).iloc[0]


def test_normal_flight_crossing_timezones(con):
    # JFK 06:00 EST + 27 min delay; LAX is 3 h behind: 06:27 → 09:51 local is 384 min, not 204
    r = one(con)
    assert r.DEPARTURE_TIME == "0627" and r.ARRIVAL_TIME == "0951" and r.ELAPSED_TIME == 384
    assert r.sched_dep_utc == ts("2015-01-13 11:00") and r.dep_utc == ts("2015-01-13 11:27")
    assert r.end_utc == ts("2015-01-13 17:51") and r.end_source == "elapsed"
    assert r.status == "normal" and list(r.dq_flags) == []


def test_departure_at_midnight_is_2400_of_the_scheduled_day(con):
    r = one(con, sched_dep="2359", delay=1)
    assert r.DEPARTURE_TIME == "2400"
    assert r.dep_utc == ts("2015-01-14 05:00") and list(r.dq_flags) == []


def test_delay_past_midnight_and_early_departure_before_midnight(con):
    late = one(con, sched_dep="2330", delay=90)          # departs 01:00 the next local day
    early = one(con, sched_dep="0005", delay=-11)        # departs 23:54 the previous local day (like AS 98)
    assert late.DEPARTURE_TIME == "0100" and late.dep_utc == ts("2015-01-14 06:00")
    assert early.DEPARTURE_TIME == "2354" and early.dep_utc == ts("2015-01-13 04:54")
    assert list(late.dq_flags) == [] and list(early.dq_flags) == []


def test_october_bts_ids_are_mapped_but_kept_as_delivered(con):
    r = one(con, day="2015-10-16", origin="JFK", dest="LAX", origin_code=bts_id("JFK"), dest_code=bts_id("LAX"))
    assert r.ORIGIN_AIRPORT == bts_id("JFK") and r.origin_iata == "JFK" and r.destination_iata == "LAX"
    assert list(r.dq_flags) == []


def test_cancelled_flight_has_no_departure_or_end(con):
    r = one(con, status="cancelled")
    assert r.status == "cancelled" and pd.isna(r.dep_utc) and pd.isna(r.end_utc)
    assert list(r.dq_flags) == []


def test_diverted_flight_end_comes_from_the_arrival(con):
    # departs JFK 06:27 EST (11:27 UTC) and reaches LAX 10 h later after the diversion: 21:27 UTC
    arrived = one(con, status="diverted", diverted_total=600)
    assert arrived.end_utc == ts("2015-01-13 21:27") and arrived.end_source == "arrival"
    overnight = one(con, status="diverted", sched_dep="2000", delay=0, diverted_total=900)
    assert overnight.end_utc == ts("2015-01-14 16:00")   # arrival clock is on the next local day
    never = one(con, status="diverted")
    assert pd.isna(never.end_utc) and pd.isna(never.end_source)
    assert all(list(r.dq_flags) == [] for r in (arrived, overnight, never))


def test_fingerprints(con):
    rows = [make_flight(), make_flight(), make_flight(delay=28), make_flight(tail=None)]
    s = stage_rows(con, rows)
    assert s.record_hash[0] == s.record_hash[1] and s.key_hash[0] == s.key_hash[1]
    assert s.record_hash[2] != s.record_hash[0] and s.key_hash[2] == s.key_hash[0]   # same flight, corrected
    assert s.key_hash[3] is not None and s.key_hash[3] != s.key_hash[0]               # no tail still hashes


@pytest.mark.parametrize("override, flag", [
    (dict(ELAPSED_TIME="400"), "rule_elapsed_parts"),
    (dict(WHEELS_OFF="0650"), "rule_wheels_off"),
    (dict(ARRIVAL_DELAY="5"), "rule_arrival_delay"),
    (dict(DEPARTURE_DELAY="30"), "rule_departure_delay"),
    (dict(AIRLINE="ZZ"), "unknown_airline"),
    (dict(DESTINATION_AIRPORT="XXX"), "unknown_airport"),
    (dict(DEPARTURE_TIME="0000"), "bad_clock"),
    (dict(DISTANCE="far"), "cast_failure"),
    (dict(ARRIVAL_TIME=None), "status_normal_missing"),
    (dict(CANCELLATION_REASON="B"), "status_reason_not_cancelled"),
])
def test_rule_violations_are_flagged(con, override, flag):
    assert flag in list(one(con, **override).dq_flags)


def test_cancelled_without_reason_is_flagged(con):
    assert "status_cancelled_fields" in list(one(con, status="cancelled", reason=None).dq_flags)


def test_timezone_mismatch_is_a_warning(con):
    # arrival clock one hour off, as on the DST nights
    assert list(one(con, ARRIVAL_TIME="1051").dq_flags) == ["rule_arrival_taxi_in", "rule_arrival_delay",
                                                           "warn_elapsed_vs_arrival"]


def test_offset_tables_follow_duckdb_rules_on_dst_dates():
    local, utc_hours = tz.offset_tables(["America/New_York"], date(2015, 3, 8), date(2015, 11, 1))
    off = local.set_index("local_hour").offset_min
    assert off[datetime(2015, 3, 8, 1)] == -300 and off[datetime(2015, 3, 8, 2)] == -300   # skipped hour: before
    assert off[datetime(2015, 3, 8, 3)] == -240
    assert off[datetime(2015, 11, 1, 1)] == -300                                              # repeated hour: later
    u = utc_hours.set_index(utc_hours.utc_hour.dt.tz_localize(None)).offset_min
    assert u[datetime(2015, 3, 8, 6)] == -300 and u[datetime(2015, 3, 8, 7)] == -240
    assert u[datetime(2015, 11, 1, 5)] == -240 and u[datetime(2015, 11, 1, 6)] == -300


def test_wrong_columns_are_rejected(con, tmp_path):
    path = tmp_path / "feed.csv"
    pd.DataFrame([make_flight()]).drop(columns=["TAXI_IN"]).to_csv(path, index=False)
    with pytest.raises(FeedFormatError, match="TAXI_IN"):
        load_raw_csv(con, path)
