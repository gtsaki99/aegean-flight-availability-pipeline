"""Command-line entry point: python -m aegean <command>."""
import argparse
from pathlib import Path

from . import config


def cmd_build_lookups(args):
    from .lookups import build_lookups
    report = build_lookups(refresh_bts=args.refresh_bts)
    print("\n".join(report.lines))
    print(f"\nLookups written to {config.LOOKUP_DIR}")


def cmd_initial_load(args):
    import time
    from . import db
    from .checks import format_report
    from .load import LoadBlocked, initial_load

    from .pipeline import set_watermark

    started = time.time()
    con = db.connect(args.db)
    try:
        results = initial_load(con, config.raw_file("flights.csv"), until=args.until)
        # the loaded history counts as processed up to its last day, so the first daily feed
        # is handled like any other (replace from W-5, compare older days, detect gaps)
        set_watermark(con, con.sql("select max(flight_date) from flights").fetchone()[0])
    except LoadBlocked as blocked:
        results = blocked.results
        print(blocked)
    report = format_report(results, f"Initial load{' up to ' + args.until if args.until else ''}")
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # a partial load (e.g. the history before a manual daily-flow test) gets its own report,
    # so it never overwrites the full-year one
    name = f"initial_load_dq_report_until_{args.until}.md" if args.until else "initial_load_dq_report.md"
    (config.OUTPUT_DIR / name).write_text(report, encoding="utf-8")
    rows, days = con.sql("select count(*), count(distinct flight_date) from flights").fetchone()
    print(report)
    print(f"flights: {rows} rows, {days} days ({time.time() - started:.0f}s)")


def cmd_availability(args):
    import time
    from . import availability, db

    started = time.time()
    con = db.connect(args.db)
    result = availability.compute(con)
    availability.export(con, config.OUTPUT_DIR)
    rows, days, airlines = con.sql("select count(*), count(distinct flight_date), count(distinct airline) "
                                   "from flights_availability").fetchone()
    print(f"flights_availability: {rows} rows ({days} days x {airlines} airlines) in {time.time() - started:.0f}s")
    print(f"written to {config.OUTPUT_DIR / 'flights_availability.csv'} and .parquet")
    return result


def cmd_full_run(args):
    """Complete 2015 output straight from flights.csv: initial load of the whole file + Task 2."""
    args.until = None
    cmd_initial_load(args)
    cmd_availability(args)


def cmd_demo(args):
    from .demo import run_demo
    ok, report = run_demo()
    print("\n".join(report))
    print(f"\nreport: {config.OUTPUT_DIR / 'demo_report.md'}")
    if not ok:
        raise SystemExit("demo: at least one scenario check FAILED")


def cmd_land(args):
    from datetime import date
    from . import db
    from .pipeline import land
    con = db.connect(args.db)
    feed_id, status = land(con, args.file, date.fromisoformat(args.as_of), config.ROOT / "landing", args.kind,
                           args.range_from, args.range_to)
    print(f"feed {feed_id}: {status}")


def cmd_process(args):
    from . import db
    from .pipeline import process
    con = db.connect(args.db)
    for feed_id, kind, as_of, status, results in process(con):
        alerts = [f"{r.name}: {r.message}" for r in results if not r.passed and r.severity in ("warn", "block")]
        print(f"{kind} {as_of} (feed {feed_id}): {status}" + "".join(f"\n  - {a}" for a in alerts))


def cmd_report(args):
    from . import db, reports
    con = db.connect(args.db)
    (config.OUTPUT_DIR / "sanity_report.md").write_text(reports.sanity_report(con), encoding="utf-8")
    chart = reports.aa_us_chart(con, config.OUTPUT_DIR / "aa_us_availability.png")
    print(f"written: {config.OUTPUT_DIR / 'sanity_report.md'}, {chart}")


def cmd_profile(args):
    import time
    from analysis import profiling
    from . import db
    started = time.time()
    con = db.connect(args.db)
    (config.OUTPUT_DIR / "profiling_report.md").write_text(profiling.run(con), encoding="utf-8")
    print(f"written: {config.OUTPUT_DIR / 'profiling_report.md'} ({time.time() - started:.0f}s)")


def cmd_rebuild(args):
    """Rebuild a warehouse from scratch: initial load + every archived feed of the source warehouse."""
    import duckdb
    from . import db
    from .pipeline import rebuild
    source = duckdb.connect(str(args.source), read_only=True)
    target = db.connect(args.target)
    outcomes = rebuild(target, source, config.raw_file("flights.csv"), args.until)
    print(f"rebuilt {args.target}: {len([o for o in outcomes if o[3] == 'PROCESSED'])} feeds processed")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m aegean")
    parser.add_argument("--raw-dir", type=Path, help="folder with flights.csv, airports.csv, airlines.csv")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("build-lookups", help="build airports (timezones, BTS IDs) and airlines lookups")
    p.add_argument("--refresh-bts", action="store_true", help="download the BTS lookups again")
    p.set_defaults(func=cmd_build_lookups)

    p = sub.add_parser("initial-load", help="load the historical flights.csv into the warehouse")
    p.add_argument("--until", help="last flight date to load (YYYY-MM-DD); default: the whole file")
    p.add_argument("--db", help="warehouse file (default warehouse/aegean.duckdb)")
    p.set_defaults(func=cmd_initial_load)

    p = sub.add_parser("availability", help="compute flights_availability (Task 2) from the warehouse")
    p.add_argument("--db", help="warehouse file (default warehouse/aegean.duckdb)")
    p.set_defaults(func=cmd_availability)

    p = sub.add_parser("full-run", help="initial load of the whole flights.csv + Task 2 output")
    p.add_argument("--db", help="warehouse file (default warehouse/aegean.duckdb)")
    p.set_defaults(func=cmd_full_run)

    p = sub.add_parser("report", help="Task 2 sanity report + AA/US chart (after full-run)")
    p.add_argument("--db", help="warehouse file (default warehouse/aegean.duckdb)")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("profile", help="profiling report: the analysis findings, recomputed (after full-run)")
    p.add_argument("--db", help="warehouse file (default warehouse/aegean.duckdb)")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("demo", help="daily pipeline demo with the simulated provider and failure scenarios")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("land", help="archive a delivered feed file and register it")
    p.add_argument("file", type=Path)
    p.add_argument("--as-of", required=True, help="feed date (YYYY-MM-DD)")
    p.add_argument("--kind", choices=["daily", "backfill"], default="daily")
    p.add_argument("--range-from", help="backfill: first day")
    p.add_argument("--range-to", help="backfill: last day")
    p.add_argument("--db", help="warehouse file (default warehouse/aegean.duckdb)")
    p.set_defaults(func=cmd_land)

    p = sub.add_parser("process", help="process every landed feed, then recompute Task 2")
    p.add_argument("--db", help="warehouse file (default warehouse/aegean.duckdb)")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("rebuild", help="rebuild a warehouse from the initial load + every archived feed")
    p.add_argument("--source", type=Path, required=True, help="warehouse whose feed_manifest lists the archived feeds")
    p.add_argument("--target", type=Path, required=True, help="new warehouse file")
    p.add_argument("--until", required=True, help="last day of the initial load (YYYY-MM-DD)")
    p.set_defaults(func=cmd_rebuild)

    args = parser.parse_args(argv)
    if args.raw_dir:
        config.RAW_DIR = args.raw_dir.resolve()
    args.func(args)
