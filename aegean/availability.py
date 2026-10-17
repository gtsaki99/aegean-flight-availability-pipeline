"""Task 2: ground time per aircraft, aggregated per airline and local day.

1. Flying intervals per aircraft (UTC):
   - normal: departure → departure + ELAPSED_TIME; diverted with an arrival: departure → arrival;
   - diverted without an arrival, and cancelled after take-off (WHEELS_OFF set): departure +
     SCHEDULED_TIME, capped at the aircraft's next departure (estimated);
   - other cancelled flights: none (the aircraft stays on the ground).
2. Overlapping intervals of one aircraft are merged, so flying time is never counted twice.
3. Ground periods = the gaps between flying blocks, plus the time from local midnight before the
   aircraft's first recorded flight and until local midnight after its last one. Each ground period
   is placed at the airport of the next departure (fallback: where the last flight landed).
4. Ground periods are split into local days of that airport, with the night window
   00:00–07:00 + 23:00–24:00 local (so 23 h / 25 h days and 7 h / 9 h nights on DST dates).
   Every day between an aircraft's first and last recorded flight counts, flights or not.
5. Each piece goes to the airline of the aircraft's previous flight while that airline still reports
   flights that day, otherwise to the airline of its next flight (handles US → AA in July 2015).

Output flights_availability (minutes): flight_date, airline, n_aircraft, availability,
night_availability, idle_day_availability (aircraft on the ground the whole day), n_cancelled,
n_diverted, estimated_minutes, is_provisional.
"""
from . import tz

CORRECTION_DAYS = 7


def _minutes(later, earlier):
    return f"(epoch({later}) - epoch({earlier})) / 60"


def _overlap(start, end, window_start, window_end):
    return f"greatest(0, {_minutes(f'least({end}, {window_end})', f'greatest({start}, {window_start})')})"


def compute(con):
    """(Re)build flights_availability and timeline_metrics from the flights table."""
    first, last = con.sql("select min(flight_date), max(flight_date) from flights").fetchone()
    tz.register(con, first, last)

    # local days per zone: start, 07:00, 23:00 and end of each day, in UTC
    con.sql("""
        create or replace temp table _days as
        select a.tz, cast(a.local_hour as date) d,
               (a.local_hour - to_minutes(a.offset_min))::timestamptz day_start,
               (b.local_hour - to_minutes(b.offset_min))::timestamptz h07,
               (c.local_hour - to_minutes(c.offset_min))::timestamptz h23,
               (e.local_hour - to_minutes(e.offset_min))::timestamptz day_end
        from tz_local_hours a
        join tz_local_hours b on b.tz = a.tz and b.local_hour = a.local_hour + interval 7 hour
        join tz_local_hours c on c.tz = a.tz and c.local_hour = a.local_hour + interval 23 hour
        join tz_local_hours e on e.tz = a.tz and e.local_hour = a.local_hour + interval 24 hour
        where hour(a.local_hour) = 0""")

    # 1. flying intervals
    con.sql("""
        create or replace temp table _iv as
        with f as (
            select TAIL_NUMBER tail, AIRLINE airline, flight_date, origin_iata org, destination_iata dst,
                   dep_utc, end_utc, SCHEDULED_TIME
            from flights
            where TAIL_NUMBER is not null and dep_utc is not null
              and (status <> 'cancelled' or WHEELS_OFF is not null)),
        n as (select *, lead(dep_utc) over (partition by tail order by dep_utc) next_dep from f)
        select tail, airline, flight_date, org, dst, dep_utc start_utc,
               coalesce(end_utc, least(dep_utc + to_minutes(SCHEDULED_TIME),
                                       coalesce(next_dep, dep_utc + to_minutes(SCHEDULED_TIME)))) end_utc,
               end_utc is null estimated
        from n""")

    # 2. merge overlapping intervals into flying blocks
    con.sql("""
        create or replace temp table _blocks as
        with o as (
            select *, max(end_utc) over (partition by tail order by start_utc, end_utc
                                         rows between unbounded preceding and 1 preceding) prev_end
            from _iv),
        g as (
            select *,
                   sum(case when prev_end is null or start_utc >= prev_end then 1 else 0 end)
                       over (partition by tail order by start_utc, end_utc rows unbounded preceding) block
            from o)
        select tail, block, min(start_utc) start_utc, max(end_utc) end_utc,
               arg_min(org, start_utc) first_org, arg_min(airline, start_utc) first_airline,
               arg_max(dst, end_utc) last_dst, arg_max(airline, end_utc) last_airline
        from g group by tail, block""")
    con.sql("""
        create or replace temp table _overlaps as
        with o as (select *, max(end_utc) over (partition by tail order by start_utc, end_utc
                                                rows between unbounded preceding and 1 preceding) prev_end from _iv)
        select flight_date, airline, count(*) as n_overlaps from o where start_utc < prev_end group by 1, 2""")

    # 3. ground periods, with the airport (and so the local clock) where the aircraft waits
    con.sql("""
        create or replace temp table _ground as
        with b as (
            select *, lead(start_utc) over w next_start, lead(first_org) over w next_org,
                   lead(first_airline) over w next_airline,
                   row_number() over w rn, count(*) over (partition by tail) nb
            from _blocks window w as (partition by tail order by block)),
        g as (
            select tail, end_utc gs, next_start ge, next_org loc, last_airline prev_airline, next_airline,
                   last_dst <> next_org chain_break
            from b where next_start is not null
            union all   -- from local midnight before the first recorded flight
            select tail, null, start_utc, first_org, null, first_airline, false from b where rn = 1
            union all   -- until local midnight after the last recorded flight
            select tail, end_utc, null, last_dst, last_airline, null, false from b where rn = nb)
        select g.*, a.TIMEZONE loc_tz from g join airports a on a.IATA_CODE = g.loc""")
    con.sql("""
        create or replace temp table _ground2 as
        with l as (
            select g.*,
                   cast(coalesce(g.gs, g.ge)::timestamp + to_minutes(u1.offset_min) as date) start_d,
                   cast((coalesce(g.ge, g.gs) - interval 1 second)::timestamp + to_minutes(u2.offset_min) as date) end_d
            from _ground g
            join tz_utc_hours u1 on u1.tz = g.loc_tz and u1.utc_hour = date_trunc('hour', coalesce(g.gs, g.ge))
            join tz_utc_hours u2 on u2.tz = g.loc_tz
             and u2.utc_hour = date_trunc('hour', coalesce(g.ge, g.gs) - interval 1 second))
        select l.* exclude (gs, ge, start_d, end_d),
               coalesce(l.gs, ds.day_start) gs,
               coalesce(l.ge, de.day_end) ge,
               case when l.gs is null then ds.d else l.start_d end start_d,
               case when l.ge is null then de.d else l.end_d end end_d
        from l
        left join _days ds on ds.tz = l.loc_tz and ds.d = l.start_d and l.gs is null
        left join _days de on de.tz = l.loc_tz and de.d = l.end_d and l.ge is null""")

    # 4. split into local days; 5. attribute each piece to an airline
    con.sql("create or replace temp table _active as select distinct AIRLINE, flight_date from flights")
    con.sql(f"""
        create or replace temp table _pieces as
        with p as (
            select g.*, unnest(generate_series(g.start_d, g.end_d, interval 1 day))::date d from _ground2 g
            where g.ge > g.gs),
        q as (
            select p.tail, p.d, p.prev_airline, p.next_airline, p.chain_break,
                   {_overlap('p.gs', 'p.ge', 'x.day_start', 'x.day_end')} ground_min,
                   {_overlap('p.gs', 'p.ge', 'x.day_start', 'x.h07')} + {_overlap('p.gs', 'p.ge', 'x.h23', 'x.day_end')} night_min,
                   {_minutes('x.day_end', 'x.day_start')} day_min
            from p join _days x on x.tz = p.loc_tz and x.d = p.d)
        select q.*, case when q.prev_airline is not null and a.AIRLINE is not null then q.prev_airline
                         else coalesce(q.next_airline, q.prev_airline) end airline
        from q left join _active a on a.AIRLINE = q.prev_airline and a.flight_date = q.d
        where ground_min > 0""")

    con.sql(f"""
        create or replace table flights_availability as
        with t as (
            select d flight_date, airline, tail, sum(ground_min) ground_min, sum(night_min) night_min,
                   max(day_min) day_min
            from _pieces group by 1, 2, 3),
        a as (
            select flight_date, airline, count(*) n_aircraft, round(sum(ground_min))::bigint availability,
                   round(sum(night_min))::bigint night_availability,
                   count(*) filter (where ground_min >= day_min) idle_day_availability
            from t group by 1, 2),
        c as (
            select flight_date, AIRLINE airline, count(*) filter (where status = 'cancelled') n_cancelled,
                   count(*) filter (where status = 'diverted') n_diverted
            from flights group by 1, 2),
        e as (
            select flight_date, airline, round(sum({_minutes('end_utc', 'start_utc')}))::bigint estimated_minutes
            from _iv where estimated group by 1, 2),
        last_day as (select max(flight_date) m from flights)
        select flight_date, airline, coalesce(a.n_aircraft, 0) n_aircraft, coalesce(a.availability, 0) availability,
               coalesce(a.night_availability, 0) night_availability,
               coalesce(a.idle_day_availability, 0) idle_day_availability,
               coalesce(c.n_cancelled, 0) n_cancelled, coalesce(c.n_diverted, 0) n_diverted,
               coalesce(e.estimated_minutes, 0) estimated_minutes,
               flight_date > (select m from last_day) - {CORRECTION_DAYS} is_provisional
        from a full join c using (flight_date, airline) left join e using (flight_date, airline)
        where flight_date between (select min(flight_date) from flights) and (select m from last_day)
        order by flight_date, airline""")

    # monitoring metrics for the daily checks: overlapping flights, and departures from another
    # airport than the last landing (chain breaks), per airline and day
    con.sql("""
        create or replace table timeline_metrics as
        with cb as (select end_d flight_date, next_airline airline, count(*) as n_chain_breaks
                    from _ground2 where chain_break group by 1, 2)
        select flight_date, airline, coalesce(o.n_overlaps, 0) as n_overlaps, coalesce(cb.n_chain_breaks, 0) as n_chain_breaks
        from _overlaps o full join cb using (flight_date, airline) order by 1, 2""")
    # optional view per airline group (airline_groups.csv): e.g. AA_GROUP = AA + US, a comparable
    # full-year series across the merger; airline codes stay as reported in flights_availability
    con.sql("""
        create or replace table flights_availability_by_group as
        select flight_date, coalesce(g.AIRLINE_GROUP, a.airline) airline_group,
               sum(n_aircraft)::bigint n_aircraft, sum(availability)::bigint availability,
               sum(night_availability)::bigint night_availability, sum(idle_day_availability)::bigint idle_day_availability,
               sum(n_cancelled)::bigint n_cancelled, sum(n_diverted)::bigint n_diverted,
               sum(estimated_minutes)::bigint estimated_minutes, bool_or(is_provisional) is_provisional
        from flights_availability a left join airline_groups g on g.AIRLINE = a.airline
        group by 1, 2 order by 1, 2""")
    return con.table("flights_availability")


def export(con, directory):
    """Write flights_availability (and the airline-group view) as CSV and Parquet."""
    directory.mkdir(parents=True, exist_ok=True)
    for table, order in (("flights_availability", "flight_date, airline"),
                         ("flights_availability_by_group", "flight_date, airline_group")):
        for fmt in ("csv", "parquet"):
            target = (directory / f"{table}.{fmt}").as_posix()
            options = "header" if fmt == "csv" else "format parquet"
            con.sql(f"copy (select * from {table} order by {order}) to '{target}' ({options})")
