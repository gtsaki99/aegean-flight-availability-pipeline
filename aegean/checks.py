"""Checks on a staged feed.

Feed checks look at the staged rows alone. History checks compare each day with earlier days
(the stored flights plus the feed itself), so they work for a daily feed and when replaying
a whole year.

Severity: 'block' stops the load, 'warn' raises an alert but the load goes on, 'info' is recorded.
"""
from dataclasses import dataclass

import pandas as pd

from .stage import STRICT_FLAGS, WARN_FLAGS

STRICT_THRESHOLD = 0.0001     # share of rows breaking a strict rule before the feed is blocked
WARN_THRESHOLD = 0.0001       # share of rows with a timezone warning before it becomes an alert
DAY_VOLUME_THRESHOLD = 0.20   # deviation of a day's flights from the same-weekday baseline
AIRLINE_VOLUME_THRESHOLD = 0.30
AIRLINE_VOLUME_MIN_BASELINE = 50
TAIL_TRANSITIONS_ALERT = 20   # aircraft changing airline on one day


@dataclass
class CheckResult:
    name: str
    severity: str        # block / warn / info
    passed: bool
    message: str
    value: float = None

    @property
    def blocking(self):
        return self.severity == "block" and not self.passed


def _one(con, sql):
    return con.sql(sql).fetchone()


def feed_checks(con, expected_days=None, stage="stage"):
    """Checks on the staged rows: coverage, uniqueness, row rules."""
    results = []
    n_rows = _one(con, f"select count(*) from {stage}")[0]

    if expected_days is not None:
        present = {r[0] for r in con.sql(f"select distinct flight_date from {stage}").fetchall()}
        expected = set(expected_days)
        missing, extra = sorted(expected - present), sorted(present - expected)
        results.append(CheckResult(
            "coverage", "block", not missing and not extra,
            f"expected {len(expected)} days {min(expected)}..{max(expected)}; missing {missing}; unexpected {extra}"))

    dup_keys, dup_rows = _one(con, f"""select count(*) - count(distinct key_hash),
                                              count(*) - count(distinct record_hash) from {stage}""")
    results.append(CheckResult("unique_key", "block", dup_keys == 0,
                               f"{dup_keys} rows share an extended key with another row", dup_keys))
    results.append(CheckResult("exact_duplicates", "block", dup_rows == 0,
                               f"{dup_rows} rows are exact copies of another row", dup_rows))

    counts = dict(con.sql(f"select flag, count(*) from (select unnest(dq_flags) flag from {stage}) group by 1").fetchall())
    for flag in STRICT_FLAGS:
        n = counts.get(flag, 0)
        # unknown codes block on any occurrence: a new airline or airport must be added to the lookups first
        limit = 0 if flag in ("unknown_airline", "unknown_airport") else STRICT_THRESHOLD * n_rows
        results.append(CheckResult(flag, "block", n <= limit,
                                   f"{n} of {n_rows} rows ({n / max(n_rows, 1):.4%})", n))
    for flag in WARN_FLAGS:
        n = counts.get(flag, 0)
        results.append(CheckResult(flag, "warn", n <= WARN_THRESHOLD * n_rows,
                                   f"{n} of {n_rows} rows ({n / max(n_rows, 1):.4%})", n))
    return results


def _daily_history(con, days, stage):
    """Flights per airline and day: stored flights for other days + the staged rows for `days`."""
    con.register("_check_days", pd.DataFrame({"flight_date": pd.to_datetime(list(days)).date}))
    con.sql(f"""
        create or replace temp table _counts as
        select flight_date, AIRLINE, count(*) n from (
            select flight_date, AIRLINE from flights where flight_date not in (select flight_date from _check_days)
            union all
            select flight_date, AIRLINE from {stage} where flight_date in (select flight_date from _check_days))
        group by all""")


def history_checks(con, days, stage="stage"):
    """Compare each of `days` with earlier days: volume, airline codes, aircraft changing airline.

    Baseline = the same weekday in the previous 4 weeks (at least 2 of them present).
    """
    days = sorted(days)
    _daily_history(con, days, stage)
    results = []

    # baselines skip holidays, so a normal day isn't compared with e.g. Thanksgiving
    same_weekday = """p.flight_date in (t.flight_date - 7, t.flight_date - 14, t.flight_date - 21, t.flight_date - 28)
                      and p.flight_date not in (select DATE::date from holidays)"""
    total = con.sql(f"""
        with d as (select flight_date, sum(n) n from _counts group by 1),
        b as (select t.flight_date, t.n, avg(p.n) baseline, count(p.n) as n_weeks
              from d t join d p on {same_weekday}
              where t.flight_date in (select flight_date from _check_days) group by 1, 2)
        select b.flight_date, b.n, b.baseline, h.NAME holiday from b
        left join holidays h on h.DATE::date = b.flight_date
        where n_weeks >= 2 and abs(b.n / b.baseline - 1) > {DAY_VOLUME_THRESHOLD} order by 1""").fetchall()
    for day, n, baseline, holiday in total:
        results.append(CheckResult(
            f"day_volume:{day}", "info" if holiday else "warn", False,
            f"{day}: {n} flights vs same-weekday baseline {baseline:.0f} ({n / baseline - 1:+.0%})"
            + (f"; holiday: {holiday}" if holiday else "")))

    gone = con.sql("""
        with a as (select distinct AIRLINE from _counts),
        grid as (select d.flight_date, a.AIRLINE from _check_days d cross join a)
        select g.flight_date, g.AIRLINE from grid g
        left join _counts c on c.flight_date = g.flight_date and c.AIRLINE = g.AIRLINE
        where c.n is null and (select count(*) from _counts p where p.AIRLINE = g.AIRLINE
              and p.flight_date between g.flight_date - 28 and g.flight_date - 1) = 28
        order by 1, 2""").fetchall()
    for day, airline in gone:
        results.append(CheckResult(f"airline_disappeared:{day}:{airline}", "warn", False,
                                   f"{day}: {airline} has no flights but flew on each of the previous 28 days"))

    # volume per airline group: once a merger is recorded in airline_groups (e.g. US -> AA_GROUP)
    # the group's series is continuous and doesn't alert; the merger itself is caught by the
    # airline_disappeared and tail_airline_change checks
    jumps = con.sql(f"""
        with g as (select c.flight_date, coalesce(ag.AIRLINE_GROUP, c.AIRLINE) grp, sum(c.n) n from _counts c
                   left join airline_groups ag on ag.AIRLINE = c.AIRLINE group by 1, 2),
        b as (select t.flight_date, t.grp, t.n, avg(p.n) baseline, count(p.n) as n_weeks from g t
              join g p on p.grp = t.grp and {same_weekday}
              where t.flight_date in (select flight_date from _check_days) group by 1, 2, 3)
        select b.flight_date, b.grp, b.n, b.baseline, h.NAME holiday from b
        left join holidays h on h.DATE::date = b.flight_date
        where n_weeks >= 2 and baseline >= {AIRLINE_VOLUME_MIN_BASELINE} and abs(n / baseline - 1) > {AIRLINE_VOLUME_THRESHOLD}
        order by 1, 2""").fetchall()
    for day, group, n, baseline, holiday in jumps:
        results.append(CheckResult(
            f"airline_volume:{day}:{group}", "info" if holiday else "warn", False,
            f"{day}: {group} {n} flights vs baseline {baseline:.0f} ({n / baseline - 1:+.0%})"
            + (f"; holiday: {holiday}" if holiday else "")))

    unknown = con.sql(f"""select distinct AIRLINE from {stage} where AIRLINE not in (select AIRLINE from airline_groups)""").fetchall()
    results.append(CheckResult("airline_not_in_groups", "warn", not unknown,
                               f"airlines missing from airline_groups: {[u[0] for u in unknown]}"))

    # aircraft flying for another airline than on their previous flight (e.g. US -> AA on 2015-07-01)
    con.sql(f"""
        create or replace temp table _tails as
        select TAIL_NUMBER, flight_date, AIRLINE, dep from (
            select TAIL_NUMBER, flight_date, AIRLINE, coalesce(dep_utc, sched_dep_utc) dep from flights
            where TAIL_NUMBER is not null and flight_date not in (select flight_date from _check_days)
            union all
            select TAIL_NUMBER, flight_date, AIRLINE, coalesce(dep_utc, sched_dep_utc) from {stage}
            where TAIL_NUMBER is not null and flight_date in (select flight_date from _check_days))""")
    shared = con.sql("""select flight_date, count(*) from (select TAIL_NUMBER, flight_date from _tails
                        where flight_date in (select flight_date from _check_days)
                        group by all having count(distinct AIRLINE) > 1) group by 1 order by 1""").fetchall()
    for day, n in shared:
        results.append(CheckResult(f"tail_two_airlines:{day}", "warn", False,
                                   f"{day}: {n} aircraft fly for more than one airline on the same day"))
    moves = con.sql("""
        with o as (select *, lag(AIRLINE) over (partition by TAIL_NUMBER order by dep) prev from _tails)
        select flight_date, prev, AIRLINE, count(*) n from o
        where prev <> AIRLINE and flight_date in (select flight_date from _check_days)
        group by all order by 1, n desc""").fetchall()
    for day, prev, airline, n in moves:
        results.append(CheckResult(f"tail_airline_change:{day}:{prev}->{airline}",
                                   "warn" if n >= TAIL_TRANSITIONS_ALERT else "info", False,
                                   f"{day}: {n} aircraft move from {prev} to {airline}", n))
    con.unregister("_check_days")
    return results


METRIC_MIN_COUNT = 10        # ignore small numbers
METRIC_FACTOR = 3            # alert when a day is 3x the airline's median of the previous 28 days


def metric_checks(con, day):
    """Overlapping flights and chain breaks per airline on `day` vs the airline's recent level.

    These come from the Task 2 timelines (timeline_metrics). The counts aren't interpreted (e.g. a
    chain break may be an unrecorded international or ferry flight); only a sudden jump alerts,
    which would point to a change in how an airline reports its flights.
    """
    rows = con.sql(f"""
        with t as (select * from timeline_metrics where flight_date = date '{day}'),
        h as (select airline, median(n_overlaps) med_overlaps, median(n_chain_breaks) med_breaks from timeline_metrics
              where flight_date between date '{day}' - 28 and date '{day}' - 1 group by 1)
        select t.airline, t.n_overlaps, h.med_overlaps, t.n_chain_breaks, h.med_breaks from t join h using (airline)""").fetchall()
    results = []
    for airline, overlaps, med_o, breaks, med_b in rows:
        for name, value, median in (("overlaps", overlaps, med_o), ("chain_breaks", breaks, med_b)):
            if value >= METRIC_MIN_COUNT and value > METRIC_FACTOR * max(median or 0, 1):
                results.append(CheckResult(f"{name}:{day}:{airline}", "warn", False,
                                           f"{day}: {airline} {value} {name.replace('_', ' ')} vs median {median:.0f}", value))
    results.append(CheckResult("timeline_metrics", "info", True, f"{day}: overlaps and chain breaks checked for {len(rows)} airlines"))
    return results


def format_report(results, title):
    """Markdown summary: blocking failures first, then alerts, then the passed checks."""
    lines = [f"# {title}", ""]
    blocked = [r for r in results if r.blocking]
    alerts = [r for r in results if not r.passed and r.severity == "warn"]
    infos = [r for r in results if not r.passed and r.severity == "info"]
    passed = [r for r in results if r.passed]
    lines.append(f"- Blocking failures: {len(blocked)}; alerts: {len(alerts)}; info: {len(infos)}; passed: {len(passed)}")
    for heading, group in (("Blocking failures", blocked), ("Alerts", alerts), ("Info", infos), ("Passed", passed)):
        if group:
            lines += ["", f"## {heading}", ""]
            lines += [f"- **{r.name}**: {r.message}" for r in group]
    return "\n".join(lines) + "\n"
