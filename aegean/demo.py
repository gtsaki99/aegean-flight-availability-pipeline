"""End-to-end demo of the daily pipeline with the simulated provider.

Initial load up to 2015-06-30, then one run per day from 2015-07-01 to 2015-08-04 with these
scenarios (each expected outcome is verified at the end):

  2015-07-01  first feed: the US → AA switch happens on this day (real data)
  2015-07-03  feed changes an already-final day (2015-06-25)   → reconciliation alert, day not written
  2015-07-06  truncated feed (last days missing)               → quarantined, next feed covers it
  2015-07-08  schema change (extra column)                     → quarantined
  2015-07-09  the 2015-07-07 file is delivered again           → DUPLICATE (checksum)
  2015-07-11  provider down (Sat)  ┐ 2 missed feeds            → no data lost, no gap
  2015-07-12  provider down (Sun)  ┘
  2015-07-15  an old feed (2015-07-03) arrives again, reordered → SKIPPED_OLD (watermark)
  2015-07-20..24 provider down (5 missed feeds)                → coverage gap on 2015-07-25
  2015-07-26  backfill extract for the gap days                → unverified days cleared
  2015-07-27..28 processing outage (feeds land, no processing) → caught up on 2015-07-29

Afterwards: the warehouse must equal the original file for every final day, and a rebuild
from the archive must give the same flights and flights_availability.
"""
import shutil
import time
from datetime import date, timedelta

import duckdb

from . import availability, config, db
from .load import DIGEST_SQL, initial_load
from .pipeline import LandError, land, process, rebuild, set_watermark, watermark
from .simulator import Simulator
from .stage import load_raw_csv, stage

INITIAL_UNTIL = date(2015, 6, 30)
FIRST, LAST = date(2015, 7, 1), date(2015, 8, 4)
MISSED = {date(2015, 7, 11), date(2015, 7, 12)} | {date(2015, 7, 20) + timedelta(days=i) for i in range(5)}
PROCESSING_OUTAGE = {date(2015, 7, 27), date(2015, 7, 28)}


def _deliveries(sim, day, delivered):
    """Files the provider sends on `day`: (path, as_of, kind, range_from, range_to, note)."""
    if day in MISSED:
        return []
    if day == date(2015, 7, 3):
        files = [(sim.history_changed_feed(day, date(2015, 6, 25)), day, "daily", None, None, "changes final day 2015-06-25")]
    elif day == date(2015, 7, 6):
        files = [(sim.truncated_feed(day), day, "daily", None, None, "truncated")]
    elif day == date(2015, 7, 8):
        files = [(sim.schema_changed_feed(day), day, "daily", None, None, "extra column")]
    else:
        files = [(sim.daily_feed(day), day, "daily", None, None, "")]
    if day == date(2015, 7, 9):
        files.append((delivered[date(2015, 7, 7)], date(2015, 7, 7), "daily", None, None, "2015-07-07 file sent again"))
    if day == date(2015, 7, 15):
        old = date(2015, 7, 3)
        files.append((sim.daily_feed(old, name=f"feed_{old}_resent.csv.gz", reorder=True), old, "daily", None, None,
                      "old feed sent again, reordered"))
    if day == date(2015, 7, 26):
        files.append((sim.extract(date(2015, 7, 14), date(2015, 7, 15)), day, "backfill",
                      date(2015, 7, 14), date(2015, 7, 15), "backfill for the gap"))
    return files


def _truth_digests(con, first, last):
    load_raw_csv(con, config.raw_file("flights.csv"), table="raw_truth",
                 where=f"make_date(cast(YEAR as int), cast(MONTH as int), cast(DAY as int)) between date '{first}' and date '{last}'")
    stage(con, feed_id=-1, raw="raw_truth", out="truth")
    return con.sql(f"""select t.flight_date, t.digest = d.digest same from
                       (select flight_date, {DIGEST_SQL} from truth group by 1) t
                       left join day_digest d using (flight_date) order by 1""").fetchall()


def run_demo(workdir=None, log=print):
    started = time.time()
    workdir = workdir or config.ROOT / "demo"
    if workdir.exists():
        shutil.rmtree(workdir)
    outbox, archive = workdir / "provider", workdir / "landing"
    db_path, rebuild_path = workdir / "demo.duckdb", workdir / "rebuild.duckdb"
    workdir.mkdir(parents=True)
    report = ["# Daily pipeline demo", "", f"Initial load up to {INITIAL_UNTIL}; daily runs {FIRST} … {LAST} (n = 10).", ""]

    con = db.connect(db_path)
    initial_load(con, config.raw_file("flights.csv"), until=INITIAL_UNTIL.isoformat())
    set_watermark(con, INITIAL_UNTIL)
    log(f"initial load done ({time.time() - started:.0f}s)")
    sim = Simulator(config.raw_file("flights.csv"), outbox, FIRST, LAST)

    report += ["## Runs", "", "| Day | Delivered | Outcome | Alerts |", "|---|---|---|---|"]
    delivered = {}
    for i in range((LAST - FIRST).days + 1):
        day = FIRST + timedelta(days=i)
        files = _deliveries(sim, day, delivered)
        landed = []
        for path, as_of, kind, r_from, r_to, note in files:
            try:
                feed_id, status = land(con, path, as_of, archive, kind, r_from, r_to)
                landed.append(f"{kind} {as_of} → {status}" + (f" ({note})" if note else ""))
            except LandError as error:
                landed.append(f"land failed: {error}")
            if kind == "daily" and as_of == day:
                delivered[day] = path
        if not files:
            landed.append("nothing (provider down)")
        if day in PROCESSING_OUTAGE:
            outcome, alerts = "processing outage: not run", ""
        else:
            outcomes = process(con, compute_availability=(day == LAST))
            outcome = "; ".join(f"{kind} {as_of}: {status}" for _, kind, as_of, status, _ in outcomes if kind != "metrics") or "nothing to process"
            alerts = "; ".join(sorted({r.name.split(":")[0] for o in outcomes for r in o[4] if not r.passed and r.severity in ("warn", "block")}))
        report.append(f"| {day} | {'<br>'.join(landed)} | {outcome} | {alerts} |")
        log(f"{day}: {outcome}")

    # --- verification ----------------------------------------------------------------------------
    report += ["", "## Scenario checks", "", "| Scenario | Expected | Result |", "|---|---|---|"]
    manifest = {r[0]: r for r in con.sql("""select feed_id, kind, as_of, status, message, source_name from feed_manifest
                                            order by feed_id""").fetchall()}

    def status_of(source):
        return [r[3] for r in manifest.values() if r[5] == source]

    def result(feed_as_of, name_prefix):
        return con.sql(f"""select count(*) from dq_results r join feed_manifest m using (feed_id)
                           where m.as_of = date '{feed_as_of}' and m.kind = 'daily' and r.name like '{name_prefix}%'
                           and not r.passed""").fetchone()[0] > 0

    checks = [
        ("US → AA switch detected on 2015-07-01", "airline disappeared + aircraft moves", result("2015-07-01", "airline_disappeared") and result("2015-07-01", "tail_airline_change")),
        ("Final day changed (feed 2015-07-03)", "reconciliation alert", result("2015-07-03", "reconciliation")),
        ("Truncated feed 2015-07-06", "QUARANTINED", status_of("feed_2015-07-06.csv.gz") == ["QUARANTINED"]),
        ("Schema change 2015-07-08", "QUARANTINED", status_of("feed_2015-07-08.csv.gz") == ["QUARANTINED"]),
        ("2015-07-07 file sent twice", "DUPLICATE", status_of("feed_2015-07-07.csv.gz") == ["PROCESSED", "DUPLICATE"]),
        ("Old feed sent again on 2015-07-15", "SKIPPED_OLD", status_of("feed_2015-07-03_resent.csv.gz") == ["SKIPPED_OLD"]),
        ("2 missed feeds (weekend)", "no coverage gap on 2015-07-13", not result("2015-07-13", "coverage_gap")),
        ("5 missed feeds", "coverage gap on 2015-07-25", result("2015-07-25", "coverage_gap")),
        ("Backfill 2015-07-26", "PROCESSED, no unverified days left",
         status_of("extract_2015-07-14_2015-07-15.csv.gz") == ["PROCESSED"]
         and con.sql("select count(*) from dq_status").fetchone()[0] == 0),
        ("Processing outage 2015-07-27..28", "caught up, watermark = last day", watermark(con) == LAST),
    ]

    final_last = LAST - timedelta(days=6)   # the version sent at age 6 is final
    digests = _truth_digests(con, INITIAL_UNTIL - timedelta(days=9), final_last)
    matching = sum(1 for _, same in digests if same)
    checks.append((f"Final days {digests[0][0]} … {final_last} equal the original file", f"{len(digests)} of {len(digests)} days",
                   matching == len(digests)))

    log("rebuilding from the archive…")
    con.close()
    source = duckdb.connect(str(db_path), read_only=True)
    rebuilt = db.connect(rebuild_path)
    rebuild(rebuilt, source, config.raw_file("flights.csv"), INITIAL_UNTIL.isoformat())
    source.close()
    rebuilt.sql(f"attach '{db_path.as_posix()}' as demo (read_only)")
    con = rebuilt
    same_days = con.sql("""select count(*) from day_digest a join demo.day_digest b using (flight_date)
                           where a.digest = b.digest""").fetchone()[0]
    n_days = con.sql("select count(*) from demo.day_digest").fetchone()[0]
    only_one = con.sql("""select (select count(*) from (select * from flights_availability except select * from demo.flights_availability))
                               + (select count(*) from (select * from demo.flights_availability except select * from flights_availability))""").fetchone()[0]
    checks.append(("Rebuild from the archive", "same flights (all days) and same flights_availability",
                   same_days == n_days and only_one == 0))

    for scenario, expected, ok in checks:
        report.append(f"| {scenario} | {expected} | {'✓' if ok else '✗ FAILED'} |")
    report += ["", f"Final days compared with the original file: {matching}/{len(digests)} identical. "
               f"Days after {final_last} are still inside the correction window of the last feed.",
               f"Rebuild: {same_days}/{n_days} day digests identical; flights_availability rows differing: {only_one}.",
               f"Total run time: {time.time() - started:.0f}s."]
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (config.OUTPUT_DIR / "demo_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return all(ok for _, _, ok in checks), report
