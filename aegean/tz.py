"""Hourly UTC-offset tables for fast local ↔ UTC conversion.

DuckDB's timezone() costs ~6 µs per call, and staging needs about eight conversions per row.
Instead we precompute each zone's UTC offset per hour and convert with a hash join:
    tz_local_hours(tz, local_hour, offset_min)   local wall-clock hour → offset
    tz_utc_hours(tz, utc_hour, offset_min)       UTC hour → offset
This is exact as long as offset changes happen on whole hours, which is asserted.

Local times that don't exist (the skipped hour on the spring-forward date) use the offset
before the change; local times that occur twice (the repeated hour on the fall-back date)
use the later, standard-time offset. This matches DuckDB's timezone().
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pandas as pd


def _minutes(td):
    return int(td.total_seconds() // 60)


def offset_tables(zones, start, end):
    """Build both tables for `zones` between two dates (inclusive), with 3 days of margin."""
    start = datetime.combine(start, datetime.min.time()) - timedelta(days=3)
    hours = int((datetime.combine(end, datetime.min.time()) + timedelta(days=4) - start).total_seconds() // 3600)
    local_rows, utc_rows = [], []
    for name in zones:
        zone = ZoneInfo(name)
        for h in range(hours):
            local = start + timedelta(hours=h)
            before = _minutes(local.replace(tzinfo=zone, fold=0).utcoffset())
            after = _minutes(local.replace(tzinfo=zone, fold=1).utcoffset())
            # before > after: repeated hour (fall back) → later offset; otherwise gap or normal → earlier
            local_rows.append((name, local, after if before > after else before))

            utc = (start + timedelta(hours=h)).replace(tzinfo=dt_timezone.utc)
            at_start = _minutes(utc.astimezone(zone).utcoffset())
            at_end = _minutes((utc + timedelta(minutes=59)).astimezone(zone).utcoffset())
            if at_start != at_end:
                raise ValueError(f"{name} changes offset within the UTC hour {utc}: hourly table not exact")
            utc_rows.append((name, utc, at_start))
    local_df = pd.DataFrame(local_rows, columns=["tz", "local_hour", "offset_min"])
    utc_df = pd.DataFrame(utc_rows, columns=["tz", "utc_hour", "offset_min"])
    return local_df, utc_df


def register(con, start, end):
    """Create the two offset tables in `con` for every zone in the airports lookup."""
    zones = [r[0] for r in con.sql("select distinct TIMEZONE from airports").fetchall()]
    local_df, utc_df = offset_tables(zones, start, end)
    con.register("_tz_local", local_df)
    con.register("_tz_utc", utc_df)
    con.sql("create or replace temp table tz_local_hours as select tz, local_hour::timestamp local_hour, offset_min from _tz_local")
    con.sql("create or replace temp table tz_utc_hours as select tz, utc_hour::timestamptz utc_hour, offset_min from _tz_utc")
    con.unregister("_tz_local")
    con.unregister("_tz_utc")
