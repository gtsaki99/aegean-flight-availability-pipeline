"""Build raw feed rows whose fields are all consistent, so a test only changes what it tests.

Times are given as local scheduled departure + minutes; every clock value is derived in the
airport's own timezone using the real airports lookup (HHMM, midnight written as '2400').
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from aegean import config
from aegean.schema import SOURCE_NAMES

_AIRPORTS = pd.read_csv(config.LOOKUP_DIR / "airports.csv", dtype=str).set_index("IATA_CODE")
UTC = ZoneInfo("UTC")


def zone(code):
    return ZoneInfo(_AIRPORTS.loc[code, "TIMEZONE"])


def bts_id(code):
    return _AIRPORTS.loc[code, "BTS_AIRPORT_ID"]


def hhmm(local):
    minutes = local.hour * 60 + local.minute
    return "2400" if minutes == 0 else f"{minutes // 60:02d}{minutes % 60:02d}"


def utc(day, hhmm_value, code, plus=0):
    """Local HHMM on `day` at airport `code` (+ minutes) → aware UTC datetime."""
    minutes = int(hhmm_value[:2]) * 60 + int(hhmm_value[2:]) + plus
    local = datetime.combine(day, datetime.min.time()) + timedelta(minutes=minutes)
    return local.replace(tzinfo=zone(code)).astimezone(UTC)


def local(instant, code):
    return instant.astimezone(zone(code))


def make_flight(day="2015-01-13", airline="AA", number=1, tail="N1AA", origin="JFK", dest="LAX",
                sched_dep="0600", delay=27, taxi_out=20, air=354, taxi_in=10, sched_time=380,
                status="normal", reason="B", diverted_total=None, origin_code=None, dest_code=None, **override):
    """One raw row as a dict of strings (None = empty field).

    status: normal / cancelled / diverted. For a diverted flight, diverted_total is the minutes from
    departure to the eventual arrival at the destination (None = never arrived).
    origin_code / dest_code: the value written in the file (e.g. an October BTS ID).
    """
    d = date.fromisoformat(day)
    sched_dep_utc = utc(d, sched_dep, origin)
    row = dict(
        YEAR=d.year, MONTH=d.month, DAY=d.day, DAY_OF_WEEK=d.isoweekday(), AIRLINE=airline, FLIGHT_NUMBER=number,
        TAIL_NUMBER=tail, ORIGIN_AIRPORT=origin_code or origin, DESTINATION_AIRPORT=dest_code or dest,
        SCHEDULED_DEPARTURE=sched_dep, SCHEDULED_TIME=sched_time, DISTANCE=1000,
        SCHEDULED_ARRIVAL=hhmm(local(sched_dep_utc + timedelta(minutes=sched_time), dest)),
        DIVERTED=int(status == "diverted"), CANCELLED=int(status == "cancelled"))
    if status == "cancelled":
        row["CANCELLATION_REASON"] = reason
    else:
        dep_utc = utc(d, sched_dep, origin, plus=delay)
        row.update(DEPARTURE_TIME=hhmm(local(dep_utc, origin)), DEPARTURE_DELAY=delay, TAXI_OUT=taxi_out,
                   WHEELS_OFF=hhmm(local(dep_utc + timedelta(minutes=taxi_out), origin)))
        sched_arr_utc = sched_dep_utc + timedelta(minutes=sched_time)
        if status == "normal":
            elapsed = taxi_out + air + taxi_in
            arr_utc = dep_utc + timedelta(minutes=elapsed)
            arr_delay = int((arr_utc - sched_arr_utc).total_seconds() // 60)
            row.update(ELAPSED_TIME=elapsed, AIR_TIME=air, TAXI_IN=taxi_in,
                       WHEELS_ON=hhmm(local(dep_utc + timedelta(minutes=taxi_out + air), dest)),
                       ARRIVAL_TIME=hhmm(local(arr_utc, dest)), ARRIVAL_DELAY=arr_delay)
            if arr_delay >= 15:
                row.update(AIR_SYSTEM_DELAY=arr_delay, SECURITY_DELAY=0, AIRLINE_DELAY=0,
                           LATE_AIRCRAFT_DELAY=0, WEATHER_DELAY=0)
        elif diverted_total is not None:
            arr_utc = dep_utc + timedelta(minutes=diverted_total)
            row.update(TAXI_IN=taxi_in, WHEELS_ON=hhmm(local(arr_utc - timedelta(minutes=taxi_in), dest)),
                       ARRIVAL_TIME=hhmm(local(arr_utc, dest)))
    row.update(override)
    return {c: (None if row.get(c) is None else str(row[c])) for c in SOURCE_NAMES}


def stage_rows(con, rows, feed_id=1):
    """Put rows into `raw` and stage them; returns the staged rows as a DataFrame."""
    from aegean.stage import stage
    con.register("_raw_rows", pd.DataFrame(rows, columns=SOURCE_NAMES, dtype=object))
    # like read_csv(all_varchar = true): every column is text, even when empty in all rows
    columns = ", ".join(f"cast({c} as varchar) as {c}" for c in SOURCE_NAMES)
    con.sql(f"create or replace temp table raw as select {columns} from _raw_rows")
    con.unregister("_raw_rows")
    stage(con, feed_id=feed_id)
    return con.sql("select * from stage").df()
