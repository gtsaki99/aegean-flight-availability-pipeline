"""Stage: read a raw feed, type it, and add derived columns, fingerprints and row flags.

Source columns are kept as delivered (October's numeric airport IDs, HHMM clock values incl.
'2400'); cleaned values go into derived columns. Rows are never dropped here: a row that
breaks a rule is kept and flagged, and checks.py decides whether the feed is blocked.
"""
from . import tz
from .schema import CAST_COLUMNS, CLOCK_COLUMNS, EXTENDED_KEY, SOURCE_COLUMNS, SOURCE_NAMES, fingerprint


class FeedFormatError(Exception):
    """The file doesn't have the expected columns: the load is blocked before staging."""


# Row flags. STRICT rules never failed in 2015, so a failure signals a change in the feed.
# WARN rules cross timezones and fail ~90 times a year, mostly on the two DST nights.
STRICT_FLAGS = [
    "cast_failure", "bad_date", "bad_clock", "unknown_airline", "unknown_airport",
    "status_both", "status_normal_missing", "status_cancelled_fields", "status_diverted_fields",
    "status_reason_not_cancelled", "unknown_cancellation_reason",
    "rule_departure_delay", "rule_elapsed_parts", "rule_wheels_off", "rule_arrival_taxi_in",
    "rule_arrival_delay", "rule_delay_causes",
]
WARN_FLAGS = ["warn_elapsed_vs_arrival", "warn_air_time_tz", "warn_scheduled_time_tz"]

DERIVED_COLUMNS = ["flight_date", "origin_iata", "destination_iata", "status", "sched_dep_utc", "dep_utc",
                   "end_utc", "end_source", "key_hash", "record_hash", "dq_flags", "feed_id", "loaded_at"]


def load_raw_csv(con, path, where=None, table="raw"):
    """Read a feed file as text into `table`, after checking its columns."""
    # hive_partitioning off: the archive folder as_of=YYYY-MM-DD/ must not become an extra column
    source = f"read_csv('{path.as_posix()}', all_varchar = true, header = true, hive_partitioning = false)"
    columns = con.sql(f"select * from {source} limit 0").columns
    if columns != SOURCE_NAMES:
        missing = [c for c in SOURCE_NAMES if c not in columns]
        extra = [c for c in columns if c not in SOURCE_NAMES]
        raise FeedFormatError(f"{path.name}: unexpected columns (missing {missing}, extra {extra}, "
                              f"or a different order)")
    filter_sql = f"where {where}" if where else ""
    con.sql(f"create or replace temp table {table} as select * from {source} {filter_sql}")


def _typed_columns():
    parts = []
    for name, kind in SOURCE_COLUMNS:
        parts.append(name if kind in ("VARCHAR", "CLOCK") else f"try_cast({name} as {kind}) as {name}")
    return ", ".join(parts)


def _cast_failures():
    checks = ", ".join(f"case when {n} is not null and try_cast({n} as {k}) is null then '{n}' end"
                       for n, k in CAST_COLUMNS)
    return f"list_filter([{checks}], x -> x is not null)"


def _bad_clock():
    return " or ".join(
        f"({c} is not null and (not regexp_full_match({c}, '[0-9]{{4}}') or substr({c}, 3, 2) > '59'"
        f" or hhmm_min({c}) not between 1 and 1440))" for c in CLOCK_COLUMNS)


def _flag_list():
    normal = "status = 'normal'"
    rules = {
        "cast_failure": "len(cast_failures) > 0",
        "bad_date": "flight_date is null",
        "bad_clock": _bad_clock(),
        "unknown_airline": "AIRLINE not in (select IATA_CODE from airlines)",
        "unknown_airport": "origin_iata is null or destination_iata is null",
        "status_both": "CANCELLED and DIVERTED",
        "status_normal_missing": f"{normal} and (DEPARTURE_TIME is null or DEPARTURE_DELAY is null or TAXI_OUT is null"
                                 " or WHEELS_OFF is null or ELAPSED_TIME is null or AIR_TIME is null or WHEELS_ON is null"
                                 " or TAXI_IN is null or ARRIVAL_TIME is null or ARRIVAL_DELAY is null)",
        "status_cancelled_fields": "CANCELLED and (CANCELLATION_REASON is null or AIR_TIME is not null"
                                   " or ELAPSED_TIME is not null or WHEELS_ON is not null or ARRIVAL_TIME is not null)",
        "status_diverted_fields": "DIVERTED and not CANCELLED and (ELAPSED_TIME is not null or AIR_TIME is not null"
                                  " or DEPARTURE_TIME is null or WHEELS_OFF is null)",
        "status_reason_not_cancelled": "not CANCELLED and CANCELLATION_REASON is not null",
        "unknown_cancellation_reason": "CANCELLATION_REASON not in (select CODE from cancellation_reasons)",
        "rule_departure_delay": "wrap(hhmm_min(DEPARTURE_TIME) - hhmm_min(SCHEDULED_DEPARTURE) - DEPARTURE_DELAY) <> 0",
        "rule_elapsed_parts": f"{normal} and ELAPSED_TIME <> TAXI_OUT + AIR_TIME + TAXI_IN",
        "rule_wheels_off": f"{normal} and wrap(hhmm_min(WHEELS_OFF) - hhmm_min(DEPARTURE_TIME) - TAXI_OUT) <> 0",
        "rule_arrival_taxi_in": f"{normal} and wrap(hhmm_min(ARRIVAL_TIME) - hhmm_min(WHEELS_ON) - TAXI_IN) <> 0",
        "rule_arrival_delay": f"{normal} and wrap(hhmm_min(SCHEDULED_ARRIVAL) + ARRIVAL_DELAY - hhmm_min(ARRIVAL_TIME)) <> 0",
        "rule_delay_causes": f"{normal} and AIR_SYSTEM_DELAY is not null and ARRIVAL_DELAY <> AIR_SYSTEM_DELAY"
                             " + SECURITY_DELAY + AIRLINE_DELAY + LATE_AIRCRAFT_DELAY + WEATHER_DELAY",
        "warn_elapsed_vs_arrival": f"{normal} and wrap(hhmm_min(ARRIVAL_TIME) - local_min(end_local_dest)) <> 0",
        "warn_air_time_tz": f"{normal} and wrap(hhmm_min(WHEELS_ON) - local_min(wheels_on_local)) <> 0",
        "warn_scheduled_time_tz": "wrap(hhmm_min(SCHEDULED_ARRIVAL) - local_min(sched_arr_local)) <> 0",
    }
    assert list(rules) == STRICT_FLAGS + WARN_FLAGS
    cases = ", ".join(f"case when {cond} then '{name}' end" for name, cond in rules.items())
    return f"list_filter([{cases}], x -> x is not null)"


def _to_utc(local, zone, alias):
    """Join clause + expression converting a local timestamp to UTC via tz_local_hours."""
    join = f"left join tz_local_hours {alias} on {alias}.tz = {zone} and {alias}.local_hour = date_trunc('hour', {local})"
    return join, f"({local} - to_minutes({alias}.offset_min))::timestamptz"


def _to_local(instant, zone, alias):
    """Join clause + expression converting a UTC instant to local wall-clock time via tz_utc_hours."""
    join = f"left join tz_utc_hours {alias} on {alias}.tz = {zone} and {alias}.utc_hour = date_trunc('hour', {instant})"
    return join, f"({instant})::timestamp + to_minutes({alias}.offset_min)"


def stage(con, feed_id, raw="raw", out="stage"):
    """Build `out` from the raw table: typed source columns + derived columns."""
    start, end = con.sql(f"""select min(d), max(d) from (select make_date(try_cast(YEAR as int), try_cast(MONTH as int),
                             try_cast(DAY as int)) d from {raw})""").fetchone()
    if start is None:
        raise FeedFormatError("feed has no rows with a valid date")
    tz.register(con, start, end)

    j_sched, sched_dep_utc = _to_utc("sched_dep_local", "otz", "o1")
    j_dep, dep_utc = _to_utc("dep_local", "otz", "o2")
    j_end, end_local_dest = _to_local("end_utc", "dtz", "d1")
    j_won, wheels_on_local = _to_local("dep_utc + to_minutes(TAXI_OUT + AIR_TIME)", "dtz", "d2")
    j_sarr, sched_arr_local = _to_local("sched_dep_utc + to_minutes(SCHEDULED_TIME)", "dtz", "d3")
    j_d0, dep_local_dest = _to_local("dep_utc", "dtz", "d4")
    arr_joins, arr_utc = zip(*(_to_utc(f"c{k}", "dtz", f"a{k}") for k in range(3)))

    con.sql(f"""
        create or replace temp table {out} as
        with t as (
            select {_typed_columns()}, {_cast_failures()} cast_failures from {raw}),
        m as (
            select t.*, make_date(YEAR, MONTH, DAY) flight_date,
                   coalesce(oi.IATA_CODE, ob.IATA_CODE) origin_iata, coalesce(oi.TIMEZONE, ob.TIMEZONE) otz,
                   coalesce(di.IATA_CODE, db.IATA_CODE) destination_iata, coalesce(di.TIMEZONE, db.TIMEZONE) dtz,
                   case when CANCELLED then 'cancelled' when DIVERTED then 'diverted' else 'normal' end status
            from t
            left join airports oi on oi.IATA_CODE = t.ORIGIN_AIRPORT
            left join airports ob on ob.BTS_AIRPORT_ID = t.ORIGIN_AIRPORT
            left join airports di on di.IATA_CODE = t.DESTINATION_AIRPORT
            left join airports db on db.BTS_AIRPORT_ID = t.DESTINATION_AIRPORT),
        l as (
            select *, row_number() over () rid,
                   flight_date::timestamp + to_minutes(hhmm_min(SCHEDULED_DEPARTURE)) sched_dep_local,
                   case when DEPARTURE_DELAY is not null
                        then flight_date::timestamp + to_minutes(hhmm_min(SCHEDULED_DEPARTURE) + DEPARTURE_DELAY) end dep_local
            from m),
        u as (
            select l.*, {sched_dep_utc} sched_dep_utc, {dep_utc} dep_utc
            from l {j_sched} {j_dep}),
        e as (
            select *, case when status = 'normal' then dep_utc + to_minutes(ELAPSED_TIME) end end_utc from u),
        -- diverted flights: ARRIVAL_TIME has no date, so take the first destination-local date
        -- (day of departure, +1, +2) whose arrival instant is after the departure
        dv as (
            select rid, dep_utc, dtz, cast({dep_local_dest} as date) d0, to_minutes(hhmm_min(ARRIVAL_TIME)) arr
            from e {j_d0} where status = 'diverted' and ARRIVAL_TIME is not null),
        dc as (
            select *, d0::timestamp + arr c0, (d0 + 1)::timestamp + arr c1, (d0 + 2)::timestamp + arr c2 from dv),
        da as (
            select rid, case when {arr_utc[0]} > dep_utc then {arr_utc[0]}
                             when {arr_utc[1]} > dep_utc then {arr_utc[1]} else {arr_utc[2]} end arr_utc
            from dc {" ".join(arr_joins)}),
        v as (
            select e.* exclude (end_utc, rid, sched_dep_local, dep_local),
                   coalesce(e.end_utc, da.arr_utc) end_utc,
                   case when status = 'normal' then 'elapsed' when da.arr_utc is not null then 'arrival' end end_source
            from e left join da using (rid)),
        w as (
            select v.*, {end_local_dest} end_local_dest, {wheels_on_local} wheels_on_local, {sched_arr_local} sched_arr_local
            from v {j_end} {j_won} {j_sarr})
        select {", ".join(SOURCE_NAMES)},
               flight_date, origin_iata, destination_iata, status, sched_dep_utc, dep_utc, end_utc, end_source,
               {fingerprint(EXTENDED_KEY)} key_hash, {fingerprint(SOURCE_NAMES)} record_hash,
               {_flag_list()} dq_flags, {int(feed_id)} feed_id, now() loaded_at
        from w""")
    return con.table(out)
