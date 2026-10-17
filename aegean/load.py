"""Load staged rows into the warehouse.

- Days in the correction window are replaced as a whole: the feed's copy of a day replaces ours
  (handles corrected, added and removed rows, and doesn't depend on the key being perfect).
- Older days in the feed are only compared with what's stored (reconciliation), never written.
- Everything is written in one transaction: a failure leaves the previous state untouched.
"""
import pandas as pd

from .checks import CheckResult, feed_checks, history_checks
from .stage import load_raw_csv, stage

CHANGE_SHARE_THRESHOLD = 0.10   # share of a day's rows changed by one feed before an alert (to be tuned on real feeds)


class LoadBlocked(Exception):
    def __init__(self, results):
        self.results = results
        failed = [r for r in results if r.blocking]
        super().__init__("load blocked: " + "; ".join(f"{r.name}: {r.message}" for r in failed))


def ensure_tables(con, stage_table="stage"):
    con.sql(f"create table if not exists flights as select * from {stage_table} limit 0")
    con.sql("""create table if not exists day_digest (
                 flight_date date primary key, row_count integer, digest varchar, feed_id integer, updated_at timestamptz)""")
    con.sql("""create table if not exists change_log (
                 feed_id integer, flight_date date, corrected integer, added integer, removed integer,
                 stored_rows integer, logged_at timestamptz)""")
    con.sql("""create table if not exists dq_results (
                 feed_id integer, name varchar, severity varchar, passed boolean, message varchar, value double,
                 logged_at timestamptz)""")


def _days_frame(days):
    return pd.DataFrame({"flight_date": pd.to_datetime(sorted(days)).date})


DIGEST_SQL = "count(*) row_count, md5(string_agg(hex(record_hash), ',' order by record_hash)) digest"


def reconcile(con, days, stage_table="stage"):
    """Days that should be final: the feed's digest must equal the stored one."""
    if not days:
        return []
    con.register("_compare_days", _days_frame(days))
    rows = con.sql(f"""
        with f as (select flight_date, {DIGEST_SQL} from {stage_table}
                   where flight_date in (select flight_date from _compare_days) group by 1)
        select c.flight_date, f.row_count, d.row_count, f.digest = d.digest same from _compare_days c
        left join f using (flight_date) left join day_digest d using (flight_date) order by 1""").fetchall()
    con.unregister("_compare_days")
    changed = [str(day) for day, _, _, same in rows if not same]
    return [CheckResult("reconciliation", "warn", not changed,
                        f"{len(rows)} final days compared; changed outside the correction window: {changed or 'none'}")]


def apply_days(con, feed_id, replace_days, stage_table="stage"):
    """Replace `replace_days` with the staged rows, logging what changed. Returns check results."""
    con.register("_replace_days", _days_frame(replace_days))
    changes = con.sql(f"""
        with old as (select flight_date, key_hash, record_hash from flights
                     where flight_date in (select flight_date from _replace_days)),
             new as (select flight_date, key_hash, record_hash from {stage_table}
                     where flight_date in (select flight_date from _replace_days))
        select coalesce(new.flight_date, old.flight_date) flight_date,
               count(*) filter (where old.key_hash is not null and new.key_hash is not null
                                and old.record_hash <> new.record_hash) corrected,
               count(*) filter (where old.key_hash is null) added,
               count(*) filter (where new.key_hash is null) removed,
               count(old.key_hash) stored_rows
        from old full join new on old.key_hash = new.key_hash
        group by 1 order by 1""").df()

    con.begin()
    try:
        con.sql("delete from flights where flight_date in (select flight_date from _replace_days)")
        con.sql(f"""insert into flights select * from {stage_table}
                    where flight_date in (select flight_date from _replace_days)
                    order by flight_date, TAIL_NUMBER, dep_utc""")
        con.sql("delete from day_digest where flight_date in (select flight_date from _replace_days)")
        con.sql(f"""insert into day_digest select flight_date, {DIGEST_SQL}, {int(feed_id)}, now() from {stage_table}
                    where flight_date in (select flight_date from _replace_days) group by 1""")
        con.register("_changes", changes)
        con.sql(f"""insert into change_log select {int(feed_id)}, flight_date, corrected, added, removed, stored_rows, now()
                    from _changes""")
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.unregister("_replace_days")

    results = []
    if changes.stored_rows.sum() > 0:
        share = (changes.corrected + changes.added + changes.removed) / changes.stored_rows.clip(lower=1)
        busy = changes[(changes.stored_rows > 0) & (share > CHANGE_SHARE_THRESHOLD)]
        results.append(CheckResult(
            "change_share", "warn", busy.empty,
            f"{int(changes.corrected.sum())} corrected, {int(changes.added.sum())} added, "
            f"{int(changes.removed.sum())} removed; days above {CHANGE_SHARE_THRESHOLD:.0%}: "
            f"{[str(d) for d in busy.flight_date] or 'none'}"))
    return results


def record_results(con, feed_id, results):
    if not results:
        return
    frame = pd.DataFrame([(feed_id, r.name, r.severity, r.passed, r.message, r.value) for r in results],
                         columns=["feed_id", "name", "severity", "passed", "message", "value"])
    con.register("_results", frame)
    con.sql("insert into dq_results select feed_id, name, severity, passed, message, value, now() from _results")
    con.unregister("_results")


def initial_load(con, path, until=None):
    """Load the historical file (optionally up to `until`) as feed 0. Every day is replaced."""
    where = None
    if until:
        where = f"make_date(try_cast(YEAR as int), try_cast(MONTH as int), try_cast(DAY as int)) <= date '{until}'"
    load_raw_csv(con, path, where=where)
    stage(con, feed_id=0)
    ensure_tables(con)
    days = [r[0] for r in con.sql("select distinct flight_date from stage where flight_date is not null").fetchall()]
    results = feed_checks(con) + history_checks(con, days)
    record_results(con, 0, results)
    if any(r.blocking for r in results):
        raise LoadBlocked(results)
    results += apply_days(con, 0, days)
    return results
