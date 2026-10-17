import duckdb
import pandas as pd
import pytest

from aegean import config
from aegean.lookups import LookupBuildError, Report, assign_timezones, resolve_airport_ids, validate_airport_ids


def flight(month, airline, number, sched, origin, dest):
    return dict(MONTH=month, AIRLINE=airline, FLIGHT_NUMBER=number, SCHEDULED_DEPARTURE=sched,
                ORIGIN_AIRPORT=origin, DESTINATION_AIRPORT=dest)


def connect(flights, bts_ids, bts_codes, airport_codes):
    con = duckdb.connect()
    con.register("flights", pd.DataFrame(flights))
    con.register("bts_ids", pd.DataFrame(bts_ids, columns=["Code", "Description"]))
    con.register("bts_codes", pd.DataFrame(bts_codes, columns=["Code", "Description"]))
    con.register("airports", pd.DataFrame({"IATA_CODE": airport_codes}))
    return con


def repeated(month, origin, dest, n, airline="XX"):
    return [flight(month, airline, str(i), "0800", origin, dest) for i in range(n)]


def test_resolve_airport_ids_rules():
    flights = (
        repeated("10", "10001", "10002", 6) + repeated("9", "AAA", "BBB", 6)
        + [flight("10", "YY", "1", "0900", "10002", "10001"), flight("9", "YY", "1", "0900", "BBB", "AAA")]
        + [flight("10", "ZZ", str(i), "1000", "10003", "10001") for i in range(6)]
        + [flight("11", "ZZ", str(i), "1000", "CCC", "AAA") for i in range(6)]
    )
    con = connect(
        flights,
        bts_ids=[("10001", "City A: Airport A"), ("10002", "City B: Airport B"), ("10003", "City C: Renamed Airport")],
        # 10002's description has two codes, only BBB is in airports.csv; 10003's code (NEW) isn't in airports.csv.
        bts_codes=[("AAA", "City A: Airport A"), ("BBB", "City B: Airport B"), ("XXB", "City B: Airport B"),
                   ("NEW", "City C: Renamed Airport")],
        airport_codes=["AAA", "BBB", "CCC"],
    )
    m = resolve_airport_ids(con).set_index("AIRPORT_ID")
    assert m.loc["10001", "IATA_CODE"] == "AAA" and m.loc["10001", "MAP_SOURCE"] == "bts"
    assert m.loc["10002", "IATA_CODE"] == "BBB" and m.loc["10002", "MAP_SOURCE"] == "bts"
    assert m.loc["10003", "IATA_CODE"] == "CCC" and m.loc["10003", "MAP_SOURCE"] == "data_vote"
    validate_airport_ids(m.reset_index(), Report())


def test_weak_vote_leaves_id_unmapped_and_validation_fails():
    flights = [flight("10", "ZZ", "1", "1000", "10003", "10001"), flight("11", "ZZ", "1", "1000", "CCC", "AAA")]
    con = connect(flights, bts_ids=[("10001", "City A: Airport A"), ("10003", "City C: Renamed")],
                  bts_codes=[("AAA", "City A: Airport A")], airport_codes=["AAA", "CCC"])
    m = resolve_airport_ids(con)
    assert m.set_index("AIRPORT_ID").loc["10003", "IATA_CODE"] is None  # 1 vote < MIN_VOTES
    with pytest.raises(LookupBuildError, match="without a code"):
        validate_airport_ids(m, Report())


def test_validation_rejects_mapping_that_is_not_one_to_one():
    m = pd.DataFrame({"AIRPORT_ID": ["1", "2"], "IATA_CODE": ["AAA", "AAA"], "DESCRIPTION": ["a", "b"],
                      "MAP_SOURCE": ["bts", "bts"], "vote_iata": [None, None], "votes": [None, None],
                      "vote_share": [None, None]})
    with pytest.raises(LookupBuildError, match="one-to-one"):
        validate_airport_ids(m, Report())


def airports_frame(rows):
    return pd.DataFrame(rows, columns=["IATA_CODE", "LATITUDE", "LONGITUDE"])


def test_timezones_from_coordinates_manual_and_fixed():
    ap = assign_timezones(airports_frame([
        ("JFK", 40.63975, -73.77893),
        ("PHX", 33.43417, -112.00806),   # Arizona: no DST
        ("ECP", None, None),             # no coordinates: manual zone
        ("GUM", 13.48345, -144.79598),   # sign error in airports.csv: corrected
    ])).set_index("IATA_CODE")
    assert ap.loc["JFK", "TIMEZONE"] == "America/New_York" and ap.loc["JFK", "TZ_SOURCE"] == "coordinates"
    assert ap.loc["PHX", "TIMEZONE"] == "America/Phoenix"
    assert ap.loc["ECP", "TIMEZONE"] == "America/Chicago" and ap.loc["ECP", "TZ_SOURCE"] == "manual"
    assert ap.loc["GUM", "TIMEZONE"] == "Pacific/Guam" and bool(ap.loc["GUM", "COORD_FIXED"])


def test_ocean_coordinates_are_rejected():
    with pytest.raises(LookupBuildError, match="generic ocean timezone"):
        assign_timezones(airports_frame([("XXX", 20.0, -150.0)]))


def test_missing_coordinates_without_manual_zone_are_rejected():
    with pytest.raises(LookupBuildError, match="without a timezone"):
        assign_timezones(airports_frame([("XXX", None, None)]))


def test_hand_maintained_lookups_are_consistent():
    airlines = pd.read_csv(config.LOOKUP_DIR / "airlines.csv")
    groups = pd.read_csv(config.LOOKUP_DIR / "airline_groups.csv")
    assert set(airlines.IATA_CODE) == set(groups.AIRLINE)
    assert groups.set_index("AIRLINE").loc["US", "AIRLINE_GROUP"] == "AA_GROUP"
    holidays = pd.read_csv(config.LOOKUP_DIR / "holidays.csv", parse_dates=["DATE"])
    assert holidays.DATE.dt.year.eq(2015).all() and holidays.DATE.is_unique
    assert pd.Timestamp("2015-11-26") in set(holidays.DATE)  # Thanksgiving, the lowest-volume day
    reasons = pd.read_csv(config.LOOKUP_DIR / "cancellation_reasons.csv")
    assert list(reasons.CODE) == ["A", "B", "C", "D"]
