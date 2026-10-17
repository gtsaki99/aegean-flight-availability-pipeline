"""Daily pipeline: Land (archive the feed exactly as delivered) and Process (checks → load → Task 2).

Land is deliberately small so it rarely fails: it stores the file write-once in the archive and
registers it in feed_manifest (a file received twice is recognised by its checksum).

Process takes every LANDED feed, oldest as_of first. For a daily feed with as_of t, and W the
as_of of the last processed daily feed (the watermark):
- days from W-5 onwards could have changed since W (the 7-day correction window of every run in
  between), so all feed days from W-5 are replaced;
- older feed days are already final: they are only compared with what's stored (reconciliation);
- days from W-5 that the feed no longer contains are a coverage gap: they are marked unverified
  and need a backfill (with n = 10 this happens after 4 or more missed runs).
A feed older than the watermark is never applied. A backfill (a one-off extract for a date range)
replaces its days and clears their unverified mark.
"""
import hashlib
from datetime import timedelta
from pathlib import Path

import pandas as pd

from . import availability
from .checks import CheckResult, feed_checks, history_checks, metric_checks
from .load import apply_days, ensure_tables, reconcile, record_results
from .stage import FeedFormatError, load_raw_csv, stage

N_DAYS = 10
CORRECTION_DAYS = 7


class LandError(Exception):
    pass


def ensure_control_tables(con):
    con.sql("""create table if not exists feed_manifest (
                 feed_id integer, kind varchar, as_of date, range_from date, range_to date, source_name varchar,
                 path varchar, checksum varchar, size_bytes bigint, status varchar, message varchar,
                 landed_at timestamptz, processed_at timestamptz)""")
    con.sql("create table if not exists pipeline_state (watermark date)")
    con.sql("create table if not exists dq_status (flight_date date, status varchar, reason varchar, feed_id integer)")


def watermark(con):
    row = con.sql("select max(watermark) from pipeline_state").fetchone()
    return row[0] if row else None


def set_watermark(con, day):
    ensure_control_tables(con)
    con.sql("delete from pipeline_state")
    con.execute("insert into pipeline_state values (?)", [day])


def _next_feed_id(con):
    return con.sql("select coalesce(max(feed_id), 0) + 1 from feed_manifest").fetchone()[0]


def _set_status(con, feed_id, status, message=None, processed=False):
    con.execute(f"""update feed_manifest set status = ?, message = ?
                    {', processed_at = now()' if processed else ''} where feed_id = ?""", [status, message, feed_id])


def land(con, source, as_of, archive_dir, kind="daily", range_from=None, range_to=None):
    """Archive `source` unchanged and register it. Returns (feed_id, status)."""
    ensure_control_tables(con)
    source = Path(source)
    if not source.exists() or source.stat().st_size == 0:
        raise LandError(f"{source} is missing or empty")
    data = source.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    feed_id = _next_feed_id(con)
    duplicate = con.execute("select feed_id from feed_manifest where checksum = ? and status <> 'DUPLICATE'",
                            [checksum]).fetchone()
    if duplicate:
        con.execute("insert into feed_manifest values (?, ?, ?, ?, ?, ?, null, ?, ?, 'DUPLICATE', ?, now(), null)",
                    [feed_id, kind, as_of, range_from, range_to, source.name, checksum, len(data),
                     f"same file as feed {duplicate[0]}"])
        return feed_id, "DUPLICATE"
    target = Path(archive_dir) / f"{kind}" / f"as_of={as_of}" / f"feed_{feed_id}_{source.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise LandError(f"{target} already exists: the archive is write-once")
    target.write_bytes(data)
    con.execute("insert into feed_manifest values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'LANDED', null, now(), null)",
                [feed_id, kind, as_of, range_from, range_to, source.name, str(target), checksum, len(data)])
    return feed_id, "LANDED"


def _days(first, last):
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _process_one(con, feed, n_days):
    feed_id, kind, as_of = int(feed.feed_id), feed.kind, feed.as_of.date() if hasattr(feed.as_of, "date") else feed.as_of
    wm = watermark(con)
    if kind == "daily" and wm is not None and as_of <= wm:
        _set_status(con, feed_id, "SKIPPED_OLD", f"as_of {as_of} is not newer than the watermark {wm}", processed=True)
        return "SKIPPED_OLD", []
    try:
        load_raw_csv(con, Path(feed.path))
    except FeedFormatError as error:
        results = [CheckResult("schema", "block", False, str(error))]
        record_results(con, feed_id, results)
        _set_status(con, feed_id, "QUARANTINED", str(error), processed=True)
        return "QUARANTINED", results

    stage(con, feed_id=feed_id)
    ensure_tables(con)
    if kind == "daily":
        expected = _days(as_of - timedelta(days=n_days - 1), as_of)
    else:
        expected = _days(pd.Timestamp(feed.range_from).date(), pd.Timestamp(feed.range_to).date())
    results = feed_checks(con, expected_days=expected)
    if any(r.blocking for r in results):
        record_results(con, feed_id, results)
        failed = "; ".join(f"{r.name}: {r.message}" for r in results if r.blocking)
        _set_status(con, feed_id, "QUARANTINED", failed, processed=True)
        return "QUARANTINED", results

    gap = []
    if kind == "daily" and wm is not None:
        changeable_from = wm - timedelta(days=CORRECTION_DAYS - 2)
        replace = [d for d in expected if d >= changeable_from]
        compare = [d for d in expected if d < changeable_from]
        gap = _days(changeable_from, expected[0] - timedelta(days=1)) if expected[0] > changeable_from else []
    else:
        replace, compare = expected, []

    if gap:
        results.append(CheckResult(
            "coverage_gap", "warn", False,
            f"{gap[0]}..{gap[-1]} may have changed while no feed was received and aren't in this feed: "
            f"marked unverified, backfill needed"))
        con.register("_gap", pd.DataFrame({"flight_date": gap}))
        con.sql(f"""insert into dq_status select flight_date, 'unverified', 'coverage gap', {feed_id} from _gap
                    where flight_date not in (select flight_date from dq_status)""")
        con.unregister("_gap")
    # history checks only on the days this feed adds, so an event alerts once, not on every
    # run while its day is inside the correction window
    new_days = [d for d in expected if wm is None or d > wm] if kind == "daily" else []
    if new_days:
        results += history_checks(con, new_days)
    results += reconcile(con, compare)
    results += apply_days(con, feed_id, replace)
    if kind == "backfill":
        con.register("_filled", pd.DataFrame({"flight_date": replace}))
        cleared = con.sql("select count(*) from dq_status where flight_date in (select flight_date from _filled)").fetchone()[0]
        con.sql("delete from dq_status where flight_date in (select flight_date from _filled)")
        con.unregister("_filled")
        results.append(CheckResult("backfill", "info", True, f"{len(replace)} days replaced; {cleared} unverified days cleared"))
    record_results(con, feed_id, results)
    _set_status(con, feed_id, "PROCESSED", None, processed=True)
    if kind == "daily":
        set_watermark(con, as_of)
    return "PROCESSED", results


def process(con, n_days=N_DAYS, compute_availability=True):
    """Process every LANDED feed (oldest as_of first); then recompute Task 2 once."""
    ensure_control_tables(con)
    pending = con.sql("select * from feed_manifest where status = 'LANDED' order by as_of, feed_id").df()
    outcomes = []
    for feed in pending.itertuples():
        status, results = _process_one(con, feed, n_days)
        outcomes.append((int(feed.feed_id), feed.kind, pd.Timestamp(feed.as_of).date(), status, results))
    if compute_availability and any(o[3] == "PROCESSED" for o in outcomes):
        availability.compute(con)
        recent = [o[2] for o in outcomes if o[3] == "PROCESSED" and o[1] == "daily"]
        if recent:
            # the day that has just become final; younger days are still rough (e.g. missing tails
            # break the aircraft chains)
            metric_results = metric_checks(con, max(recent) - timedelta(days=CORRECTION_DAYS - 1))
            record_results(con, outcomes[-1][0], metric_results)
            outcomes.append((None, "metrics", max(recent), "CHECKED", metric_results))
    return outcomes


def rebuild(con, source_con, initial_path, until):
    """Rebuild a warehouse from scratch: initial load + every archived feed, in the original order."""
    from .load import initial_load
    ensure_control_tables(con)
    initial_load(con, initial_path, until=until)
    set_watermark(con, pd.Timestamp(until).date())
    feeds = source_con.sql("select * from feed_manifest where status <> 'DUPLICATE' order by feed_id").df()
    feeds["status"], feeds["message"], feeds["processed_at"] = "LANDED", None, None
    con.register("_feeds", feeds)
    con.sql("insert into feed_manifest select * from _feeds")
    con.unregister("_feeds")
    return process(con)
