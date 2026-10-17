"""Profiling report: every analysis finding quoted in the write-up, recomputed from the warehouse.

Run after `python -m aegean full-run` (it needs the full 2015 flights table):
    python -m aegean profile
Writes output/profiling_report.md.
"""
from aegean import config, tz
from aegean.reports import md_table
from aegean.stage import STRICT_FLAGS, WARN_FLAGS

DST_WEEKENDS = "('2015-03-07', '2015-03-08', '2015-10-31', '2015-11-01')"


def run(con):
    first, last = con.sql("select min(flight_date), max(flight_date) from flights").fetchone()
    tz.register(con, first, last)
    out = ["# Profiling report", "",
           "Findings from the analysis of the 2015 flights data, recomputed from the warehouse "
           "(`python -m aegean profile`). Summarised in section 2 of the write-up.", ""]

    def section(title, note=None):
        out.extend(["", f"## {title}", ""] + ([note, ""] if note else []))

    def table(sql, note=None, floatfmt="{:,.1f}"):
        out.append(md_table(con.sql(sql).df(), floatfmt))
        out.append("")
        if note:
            out.extend([note, ""])

    def value(sql):
        return con.sql(sql).fetchone()

    # 1 -------------------------------------------------------------------------------------------
    section("1. Overview")
    table("""select count(*) "Rows", count(distinct flight_date) "Days", count(distinct AIRLINE) "Airlines",
                    count(distinct TAIL_NUMBER) "Aircraft (tails)", min(flight_date)::varchar "First day",
                    max(flight_date)::varchar "Last day" from flights""")

    # 2 -------------------------------------------------------------------------------------------
    section("2. Row key", "The given key is (YEAR, MONTH, DAY, AIRLINE, FLIGHT_NUMBER, TAIL_NUMBER, SCHEDULED_DEPARTURE).")
    table("""with k as (select YEAR, MONTH, DAY, AIRLINE, FLIGHT_NUMBER, TAIL_NUMBER, SCHEDULED_DEPARTURE from flights
                        group by all having count(*) > 1)
             select f.flight_date::varchar "Day", f.AIRLINE "Airline", f.FLIGHT_NUMBER "Flight", coalesce(f.TAIL_NUMBER, '(none)') "Tail",
                    f.origin_iata || ' → ' || f.destination_iata "Route", f.SCHEDULED_DEPARTURE "Sched. dep",
                    coalesce(f.DEPARTURE_TIME, '') "Dep", f.status "Status"
             from flights f join k using (YEAR, MONTH, DAY, AIRLINE, FLIGHT_NUMBER, SCHEDULED_DEPARTURE)
             where f.TAIL_NUMBER is not distinct from k.TAIL_NUMBER order by 1, 3, 5""",
          "Duplicate groups under the given key (2 groups, 4 rows). With ORIGIN_AIRPORT and DESTINATION_AIRPORT added:")
    table("""select count(*) - count(distinct key_hash) "Rows sharing an extended key" from flights""")

    # 3 -------------------------------------------------------------------------------------------
    section("3. October airport codes")
    table("""select MONTH "Month", count(*) "Flights",
                    count(*) filter (where regexp_full_match(ORIGIN_AIRPORT, '[0-9]+')) "Numeric origin IDs"
             from flights where MONTH between 9 and 11 group by 1 order by 1""")
    table(f"""select MAP_SOURCE "Mapping source", count(*) "IDs" from
              read_csv('{(config.LOOKUP_DIR / 'airport_id_to_iata.csv').as_posix()}', all_varchar = true) group by 1 order by 1""",
          "Full validation (one-to-one, agreement with the flight data, route coverage): `output/lookups_report.md`.")

    # 4 -------------------------------------------------------------------------------------------
    section("4. Clock values", "HHMM text as delivered; midnight is written 2400, never 0000.")
    cols = ["SCHEDULED_DEPARTURE", "DEPARTURE_TIME", "WHEELS_OFF", "WHEELS_ON", "SCHEDULED_ARRIVAL", "ARRIVAL_TIME"]
    table(" union all ".join(
        f"""select '{c}' "Column", count({c}) "Values", count(*) filter (where {c} = '2400') "2400",
                   count(*) filter (where {c} = '0000') "0000", min({c}) "Min", max({c}) "Max",
                   count(distinct {c}) "Distinct" from flights""" for c in cols))
    table("""with t as (select hhmm_min(SCHEDULED_DEPARTURE) s, DEPARTURE_DELAY d, coalesce(ELAPSED_TIME, 0) e
                        from flights where DEPARTURE_DELAY is not null)
             select max(d) "Max delay (min)", min(d) "Min delay",
                    count(*) filter (where s + d >= 1440) "Depart on the next day",
                    count(*) filter (where s + d < 0) "Depart on the previous day",
                    count(*) filter (where s + d + e >= 1440) "Arrive on the next day (origin clock)",
                    count(*) filter (where s + d + e >= 2880) "Arrive 2 days later" from t""")

    # 5 -------------------------------------------------------------------------------------------
    section("5. Daily volume")
    table("""with d as (select flight_date, count(*) n from flights group by 1)
             select min(n) "Min", quantile_cont(n, 0.05)::int "P5", median(n)::int "Median", avg(n)::int "Mean",
                    quantile_cont(n, 0.95)::int "P95", max(n) "Max", stddev(n)::int "Std dev" from d""")
    table("""with d as (select flight_date, count(*) n from flights group by 1)
             select dayname(flight_date) "Weekday", avg(n)::int "Avg flights", min(n) "Min", max(n) "Max"
             from d group by 1, isodow(flight_date) order by isodow(flight_date)""")
    table("""select flight_date::varchar "Lowest days", dayname(flight_date) "Weekday", count(*) "Flights",
                    count(*) filter (where status = 'cancelled') "Cancelled" from flights group by 1, 2 order by 3, 1 limit 6""")
    size = (config.RAW_DIR / "flights.csv").stat().st_size if (config.RAW_DIR / "flights.csv").exists() else None
    rows = value("select count(*) from flights")[0]
    if size:
        bpr = size / rows
        out.append(f"CSV size {size:,} bytes / {rows:,} rows = **{bpr:.1f} bytes per row**.")
        out.append("")
        table(f"""with d as (select flight_date, count(*) n from flights group by 1),
                  r as (select k, sum(n) over (partition by k order by flight_date rows between k - 1 preceding and current row) s,
                               row_number() over (partition by k order by flight_date) rn
                        from d, (select unnest([7, 8, 10, 14]) k))
                  select k "n (days per feed)", max(s)::int "Max rows", round(max(s) * {bpr} / 1e6, 1) "Max CSV MB"
                  from r where rn >= k group by 1 order by 1""")
    else:
        out.extend([f"*Feed sizes not computed: flights.csv not found in {config.RAW_DIR} (pass --raw-dir).*", ""])

    # 6 -------------------------------------------------------------------------------------------
    section("6. Timezones")
    table("""select TZ_SOURCE "Timezone source", count(*) "Airports", count(distinct TIMEZONE) "Zones" from airports group by 1 order by 1""")
    table("""select count(*) "Normal flights", count(*) filter (where o.TIMEZONE <> d.TIMEZONE) "Crossing a zone"
             from flights f join airports o on o.IATA_CODE = f.origin_iata join airports d on d.IATA_CODE = f.destination_iata
             where status = 'normal'""")
    table(f"""select flag "Timezone rule", count(*) "Flights", count(*) filter (where flight_date in {DST_WEEKENDS}) "On DST weekends"
              from (select flight_date, unnest(dq_flags) flag from flights) where flag like 'warn%' group by 1 order by 1""")
    table("""select o.TIMEZONE "Origin zone", count(*) "Flights",
                    round(100.0 * count(*) filter (where cast(sched_dep_utc as date) <> flight_date) / count(*), 1) "% on another UTC date"
             from flights f join airports o on o.IATA_CODE = f.origin_iata group by 1 order by 2 desc limit 6""",
          f"All flights: {value('select round(100.0 * count(*) filter (where cast(sched_dep_utc as date) <> flight_date) / count(*), 1) from flights')[0]}% "
          "depart on a different UTC date than their flight day.")

    # 7 -------------------------------------------------------------------------------------------
    section("7. Time-consistency rules", "Row flags from staging (strict rules block a feed above 0.01% of rows; warn rules alert).")
    counts = dict(con.sql("select flag, count(*) from (select unnest(dq_flags) flag from flights) group by 1").fetchall())
    import pandas as pd
    out.append(md_table(pd.DataFrame({"Rule": STRICT_FLAGS + WARN_FLAGS,
                                      "Severity": ["strict"] * len(STRICT_FLAGS) + ["warn"] * len(WARN_FLAGS),
                                      "Rows flagged": [counts.get(f, 0) for f in STRICT_FLAGS + WARN_FLAGS]})))
    out.append("")
    table("""select flight_date::varchar "Day", AIRLINE "Airline", FLIGHT_NUMBER "Flight", status "Status",
                    SCHEDULED_DEPARTURE "Sched. dep", DEPARTURE_TIME "Dep", DEPARTURE_DELAY "Delay"
             from flights where list_contains(dq_flags, 'rule_departure_delay')""", "The one strict-rule failure in 2015.")

    # 8 -------------------------------------------------------------------------------------------
    section("8. Cancelled and diverted flights")
    table("""select status "Status", count(*) "Flights", count(TAIL_NUMBER) "With tail", count(DEPARTURE_TIME) "With dep. time",
                    count(WHEELS_OFF) "With wheels-off", count(ELAPSED_TIME) "With elapsed", count(ARRIVAL_TIME) "With arrival"
             from flights group by 1 order by 2 desc""")
    table("""select CANCELLATION_REASON || ' ' || r.DESCRIPTION "Reason", count(*) "Cancelled",
                    count(DEPARTURE_TIME) filter (where WHEELS_OFF is null) "After pushback", count(WHEELS_OFF) "After wheels-off"
             from flights f join cancellation_reasons r on r.CODE = f.CANCELLATION_REASON group by 1 order by 1""")
    table("""with d as (select flight_date, count(*) n, count(*) filter (where status = 'cancelled') c,
                               count(*) filter (where status = 'diverted') v from flights group by 1)
             select round(100 * median(c / n), 2) "Median % cancelled", round(100 * max(c / n), 1) "Max % cancelled",
                    arg_max(flight_date, c / n)::varchar "Worst day", round(100 * median(v / n), 2) "Median % diverted",
                    round(100 * max(v / n), 2) "Max % diverted" from d""")
    table("""with d as (select * from flights where status = 'diverted' and ARRIVAL_TIME is null)
             select count(*) "Diverted without arrival",
                    count(*) filter (where exists (select 1 from flights x where x.flight_date = d.flight_date and x.AIRLINE = d.AIRLINE
                        and x.FLIGHT_NUMBER = d.FLIGHT_NUMBER and x.destination_iata = d.destination_iata
                        and x.origin_iata <> d.origin_iata and x.SCHEDULED_DEPARTURE = d.SCHEDULED_DEPARTURE)) "With a continuation row"
             from d""", "One of the two continuations is EV 5660 (SHV → IAH diverted, CLL → IAH continued); the other is cancelled.")
    table("""with base as (select TAIL_NUMBER, origin_iata, destination_iata, status, WHEELS_OFF, CANCELLATION_REASON,
                                  coalesce(dep_utc, sched_dep_utc) t from flights
                           where TAIL_NUMBER is not null and (status <> 'cancelled' or WHEELS_OFF is not null)),
                  w as (select *, lead(origin_iata) over (partition by TAIL_NUMBER order by t) next_org from base)
             select count(*) "Cancelled after wheels-off", count(*) filter (where next_org = origin_iata) "Next departs from origin",
                    count(*) filter (where next_org = destination_iata) "From destination",
                    count(*) filter (where next_org not in (origin_iata, destination_iata)) "From elsewhere",
                    count(*) filter (where next_org is null) "Never again in 2015"
             from w where status = 'cancelled'""")

    # 9 -------------------------------------------------------------------------------------------
    section("9. Aircraft flying for two airlines (US → AA)")
    table("""with s as (select TAIL_NUMBER, min(AIRLINE) a, max(AIRLINE) b, max(flight_date) filter (where AIRLINE = 'US') last_us,
                               min(flight_date) filter (where AIRLINE = 'AA') first_aa from flights
                        where TAIL_NUMBER is not null group by 1 having count(distinct AIRLINE) > 1)
             select a || ' / ' || b "Airlines", count(*) "Aircraft", min(first_aa)::varchar "First switch", max(first_aa)::varchar "Last switch",
                    count(*) filter (where first_aa - last_us = 1) "1 day", count(*) filter (where first_aa - last_us between 2 and 3) "2–3 days",
                    count(*) filter (where first_aa - last_us between 4 and 7) "4–7 days",
                    count(*) filter (where first_aa - last_us between 8 and 30) "8–30 days", count(*) filter (where first_aa - last_us > 30) "31+ days",
                    max(first_aa - last_us) "Longest gap" from s group by 1""")
    table("""select AIRLINE "Airline", strftime(flight_date, '%Y-%m') "Month", count(distinct TAIL_NUMBER) "Aircraft", count(*) "Flights"
             from flights where AIRLINE in ('AA', 'US') and flight_date between '2015-05-01' and '2015-08-31' group by 1, 2 order by 2, 1""",
          "Carrier code US appears only until 2015-06-30; BTS: \"American and US Airways ... began reporting joint data in July 2015.\"")

    # 10 ------------------------------------------------------------------------------------------
    section("10. Overlapping flights per aircraft (UTC)", "Intervals: normal flights and diverted flights with an arrival.")
    con.sql("""create or replace temp table _iv as
               select AIRLINE, FLIGHT_NUMBER, TAIL_NUMBER tail, origin_iata org, destination_iata dst, flight_date, dep_utc, end_utc,
                      lead(dep_utc) over w next_dep, lead(origin_iata) over w next_org, lead(destination_iata) over w next_dst,
                      lead(AIRLINE || FLIGHT_NUMBER) over w next_flight,
                      lead(origin_iata, 2) over w next2_org, lag(destination_iata) over w prev_dst
               from flights where TAIL_NUMBER is not null and end_utc is not null
               window w as (partition by TAIL_NUMBER order by dep_utc, end_utc)""")
    con.sql("create or replace temp table _ov as select *, (epoch(end_utc) - epoch(next_dep)) / 60 overlap_min from _iv where next_dep < end_utc")
    table("""select count(*) "Overlaps", round(100.0 * count(*) / (select count(*) from _iv), 3) "% of flights",
                    count(distinct tail) "Aircraft", round(sum(overlap_min))::bigint "Overlapping minutes",
                    count(*) filter (where next_org <> dst) "Next flight from another airport" from _ov""")
    table("""select case when overlap_min <= 5 then '1–5 min' when overlap_min <= 15 then '6–15 min' when overlap_min <= 60 then '16–60 min'
                         when overlap_min <= 180 then '1–3 h' else '> 3 h' end "Overlap", count(*) "Cases",
                    count(*) filter (where next_org <> dst) "Next from another airport"
             from _ov group by 1 order by min(overlap_min)""")
    table("""select o.AIRLINE "Airline", count(*) "Overlaps", round(1e4 * count(*) / any_value(t.n), 1) "Per 10k flights"
             from _ov o join (select AIRLINE, count(*) n from _iv group by 1) t using (AIRLINE) group by 1 order by 3 desc, 1 limit 5""")
    table(f"""select count(*) filter (where overlap_min between 1380 and 1500) "Near 24 h (wrong date)",
                     count(*) filter (where flight_date in {DST_WEEKENDS}) "On DST weekends",
                     count(*) filter (where next_org = org and next_dst = dst and (epoch(next_dep) - epoch(dep_utc)) / 60 <= 30) "Same flight twice",
                     count(*) filter (where next_org <> dst and prev_dst = next_org) "Current flight doesn't fit the chain",
                     count(*) filter (where next_org <> dst and next2_org = dst) "Next flight doesn't fit the chain"
              from _ov""", "Alternative explanations tested: mostly wrong tail numbers (chain test), little date/DST/duplicate effect.")

    # 11 ------------------------------------------------------------------------------------------
    section("11. Flights missing from the dataset (limitation)")
    con.sql("""create or replace temp table _gaps as
               select i.*, (epoch(next_dep) - epoch(end_utc)) / 60 gap_min, next_org <> dst chain_break,
                      hour(end_utc::timestamp + to_minutes(h.offset_min)) land_hour
               from _iv i join airports a on a.IATA_CODE = i.dst
               join tz_utc_hours h on h.tz = a.TIMEZONE and h.utc_hour = date_trunc('hour', i.end_utc)
               where next_dep >= end_utc""")
    table("""select count(*) "Gaps", count(*) filter (where chain_break) "Chain breaks",
                    round(100.0 * count(*) filter (where chain_break) / count(*), 2) "%",
                    median(gap_min) filter (where chain_break)::int "Median break gap (min)",
                    median(gap_min) filter (where not chain_break)::int "Median normal gap (min)" from _gaps""")
    table("""select dst || ' → ' || next_org "Landed → next departure", count(*) "Chain breaks" from _gaps where chain_break
             group by 1 order by 2 desc, 1 limit 6""")
    table("""select AIRLINE "Airline", round(100.0 * count(*) filter (where chain_break) / count(*), 2) "% chain breaks",
                    round(1e3 * count(*) filter (where not chain_break and land_hour between 6 and 17 and gap_min >= 600)
                          / count(*) filter (where not chain_break), 1) "Long daytime gaps per 1k",
                    round(100.0 * sum(gap_min) filter (where chain_break or (land_hour between 6 and 17 and gap_min >= 600))
                          / sum(gap_min), 1) "% of ground time in these gaps"
             from _gaps group by 1 order by 4 desc, 1""",
          "Long daytime gap: landed 06:00–17:59 local and next departure from the same airport ≥ 10 h later. "
          "Descriptive only: the cause of a single gap can't be told from the data.")

    # 12 ------------------------------------------------------------------------------------------
    section("12. Which aircraft count on a day (rule C)")
    con.sql("""create or replace temp table _td as select TAIL_NUMBER tail, flight_date, count(*) n,
               count(*) filter (where status <> 'cancelled') flown from flights where TAIL_NUMBER is not null group by 1, 2""")
    table("""with s as (select tail, min(flight_date) a, max(flight_date) b, count(*) active from _td group by 1)
             select (select count(*) from _td where flown > 0) "A: flown", (select count(*) from _td) "B: scheduled",
                    (select count(*) from _td where flown = 0) "Grounded all day by cancellations",
                    sum(b - a + 1 - active)::bigint "C adds: days without any flight" from s""")
    table("""with g as (select tail, flight_date - lag(flight_date) over (partition by tail order by flight_date) - 1 absent from _td)
             select case when absent = 1 then '1 day' when absent <= 7 then '2–7 days' when absent <= 30 then '8–30 days' else '> 30 days' end "Absence",
                    count(*) "Absences", sum(absent)::bigint "Idle days" from g where absent >= 1 group by 1 order by min(absent)""")
    table("""with d as (select flight_date, sum(n_aircraft) n, sum(idle_day_availability) idle from flights_availability group by 1),
                  m as (select median(n) mn from d where flight_date between '2015-02-01' and '2015-11-30')
             select flight_date::varchar "Day", n "Aircraft counted", round(100.0 * n / mn) "% of typical", idle "Idle aircraft" from d, m
             where flight_date in ('2015-01-01', '2015-01-07', '2015-01-14', '2015-06-15', '2015-12-18', '2015-12-24', '2015-12-31') order by 1""",
          "Data-boundary effect: an aircraft only counts from its first to its last recorded flight, so the first and last ~3 weeks "
          "of 2015 undercount the fleet (by up to 16%).")

    return "\n".join(out) + "\n"
