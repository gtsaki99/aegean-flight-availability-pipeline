"""Feed simulator: a test harness standing in for the data provider (not part of the pipeline).

The 2015 file is the final truth. The feed for day t contains the flight days t-n+1..t. Only
the 7 most recent days (t-6..t, age 0..6) may still change: the version a day has at age 6 is
final and equals the file, as do all older days. Younger days are early versions that later
feeds correct; the chance of a correction shrinks with age (yesterday is the roughest). Every correction keeps
the strict rules valid, e.g. a delay change shifts all dependent times together.

Corrections: late-arriving flights (missing at first; cancellations more often), tail number
filled in later, delay changes, diverted arrival filled in later, phantom rows (sent, then
removed). The rates are invented: the simulator shows that each kind of change is handled,
not that the rates are realistic.

Randomness is derived from md5(seed, flight key, as_of, kind), so every run is reproducible.
"""
from datetime import date, timedelta
from pathlib import Path

import duckdb

from .schema import SOURCE_NAMES

DEFAULT_RATES = {           # probability at age 0 (yesterday); scaled by (6 - age) / 6, 0 from age 6
    "late": 0.01,           # flight missing from the feed (×5 for cancellations)
    "tail": 0.005,          # tail number not filled in yet
    "delay": 0.02,          # delay changes by 5–30 minutes (normal flights)
    "diverted": 0.5,        # diverted flight's final arrival not known yet
    "phantom": 0.001,       # a row that shouldn't be there (removed later)
}
FINAL_AGE = 6

KEY = "concat_ws('|', YEAR, MONTH, DAY, AIRLINE, FLIGHT_NUMBER, coalesce(TAIL_NUMBER, ''), SCHEDULED_DEPARTURE, ORIGIN_AIRPORT, DESTINATION_AIRPORT)"


class Simulator:
    def __init__(self, truth_csv, outbox, first_day, last_day, n_days=10, seed=42, rates=None):
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.n_days, self.seed = n_days, seed
        self.rates = {**DEFAULT_RATES, **(rates or {})}
        self.con = duckdb.connect()
        self.con.sql("SET enable_progress_bar = false")
        start = first_day - timedelta(days=n_days)
        self.con.sql(f"""
            create table truth as
            select * from (select *, make_date(cast(YEAR as int), cast(MONTH as int), cast(DAY as int)) d
                           from read_csv('{Path(truth_csv).as_posix()}', all_varchar = true))
            where d between date '{start}' and date '{last_day}'""")
        self.con.sql("""create macro shift_hhmm(x, k) as case when x is null or k = 0 then x else
                        printf('%02d%02d', ((cast(substr(x, 1, 2) as int) * 60 + cast(substr(x, 3, 2) as int) + k - 1) % 1440 + 1) // 60,
                                           ((cast(substr(x, 1, 2) as int) * 60 + cast(substr(x, 3, 2) as int) + k - 1) % 1440 + 1) % 60) end""")
        self.con.sql("create macro plus(x, k) as case when x is null then null else cast(cast(x as int) + k as varchar) end")

    def _u(self, as_of, kind):
        return f"(md5_number('{self.seed}|' || {KEY} || '|{as_of}|{kind}') % 1000000) / 1e6"

    def feed_sql(self, as_of, order="d, AIRLINE, FLIGHT_NUMBER, ORIGIN_AIRPORT"):
        r = self.rates
        first = as_of - timedelta(days=self.n_days - 1)
        return f"""
            with src as (
                select *, date '{as_of}' - d age from truth where d between date '{first}' and date '{as_of}'),
            w as (
                select *, case when age < {FINAL_AGE} then ({FINAL_AGE} - age) / {FINAL_AGE}.0 else 0 end w,
                       {self._u(as_of, 'late')} u_late, {self._u(as_of, 'tail')} u_tail,
                       {self._u(as_of, 'delay')} u_delay, {self._u(as_of, 'shift')} u_shift,
                       {self._u(as_of, 'diverted')} u_div, {self._u(as_of, 'phantom')} u_phantom
                from src),
            k as (
                select *, case when CANCELLED = '0' and DIVERTED = '0' and u_delay < w * {r['delay']}
                               then 5 + cast(floor(u_shift * 26) as int) else 0 end shift,
                       DIVERTED = '1' and ARRIVAL_TIME is not null and u_div < w * {r['diverted']} arrival_unknown
                from w
                where not (u_late < w * {r['late']} * case when CANCELLED = '1' then 5 else 1 end)),
            early as (
                select * replace (
                    case when u_tail < w * {r['tail']} then null else TAIL_NUMBER end as TAIL_NUMBER,
                    shift_hhmm(DEPARTURE_TIME, shift) as DEPARTURE_TIME,
                    plus(DEPARTURE_DELAY, shift) as DEPARTURE_DELAY,
                    shift_hhmm(WHEELS_OFF, shift) as WHEELS_OFF,
                    case when arrival_unknown then null else shift_hhmm(WHEELS_ON, shift) end as WHEELS_ON,
                    case when arrival_unknown then null else TAXI_IN end as TAXI_IN,
                    case when arrival_unknown then null else shift_hhmm(ARRIVAL_TIME, shift) end as ARRIVAL_TIME,
                    plus(ARRIVAL_DELAY, shift) as ARRIVAL_DELAY,
                    case when AIR_SYSTEM_DELAY is not null then plus(AIRLINE_DELAY, shift) else AIRLINE_DELAY end as AIRLINE_DELAY)
                from k),
            phantom as (
                select * replace (cast(cast(FLIGHT_NUMBER as int) + 10000 as varchar) as FLIGHT_NUMBER,
                                  null as TAIL_NUMBER)
                from early where u_phantom < w * {r['phantom']})
            select {", ".join(SOURCE_NAMES)} from (select * from early union all select * from phantom)
            order by {order}"""

    def write(self, sql, name):
        path = self.outbox / name
        self.con.sql(f"copy ({sql}) to '{path.as_posix()}' (header, compression gzip)")
        return path

    def daily_feed(self, as_of, name=None, reorder=False):
        """The provider's feed for as_of (reorder: same content in a different row order)."""
        order = "md5(" + KEY + ")" if reorder else "d, AIRLINE, FLIGHT_NUMBER, ORIGIN_AIRPORT"
        return self.write(self.feed_sql(as_of, order), name or f"feed_{as_of}.csv.gz")

    def extract(self, first, last, name=None):
        """One-off extract of final data for a date range (used for backfills)."""
        sql = f"""select {", ".join(SOURCE_NAMES)} from truth where d between date '{first}' and date '{last}'
                  order by d, AIRLINE, FLIGHT_NUMBER, ORIGIN_AIRPORT"""
        return self.write(sql, name or f"extract_{first}_{last}.csv.gz")

    # --- broken deliveries for the failure scenarios --------------------------------------------

    def truncated_feed(self, as_of, keep=0.6):
        n = self.con.sql(f"select count(*) from ({self.feed_sql(as_of)})").fetchone()[0]
        return self.write(f"select * from ({self.feed_sql(as_of)}) limit {int(n * keep)}", f"feed_{as_of}.csv.gz")

    def schema_changed_feed(self, as_of):
        return self.write(f"select *, 'x' as NEW_COLUMN from ({self.feed_sql(as_of)})", f"feed_{as_of}.csv.gz")

    def history_changed_feed(self, as_of, final_day):
        """A feed in which the rows of an already-final day differ from what was sent before.

        DISTANCE is changed by one mile: a real change that reconciliation must catch, without
        touching any time rule."""
        return self.write(f"""
            select * replace (
                case when make_date(cast(YEAR as int), cast(MONTH as int), cast(DAY as int)) = date '{final_day}'
                     then plus(DISTANCE, 1) else DISTANCE end as DISTANCE)
            from ({self.feed_sql(as_of)})""", f"feed_{as_of}.csv.gz")
