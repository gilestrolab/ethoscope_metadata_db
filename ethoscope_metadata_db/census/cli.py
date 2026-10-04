"""``ethocensus``: command-line front end to the ethoscope census.

Every location comes from the command line; nothing is hard-coded. A typical
run::

    ethocensus --census census.sqlite scan-dbs /mnt/ethoscope_results
    ethocensus --census census.sqlite discover /home
    ethocensus --census census.sqlite ingest
    ethocensus --census census.sqlite match
    ethocensus --census census.sqlite report --out-dir census_report/
    ethocensus --census census.sqlite query "genotype == 'CS'" --out cs.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from . import discover as discover_mod
from . import match as match_mod
from . import report as report_mod
from . import store
from .dbindex import scan_results


def _print_counts(title: str, counts: dict[str, int]) -> None:
    print(title + ": " + ", ".join(f"{k}={v}" for k, v in counts.items()))


def cmd_scan_dbs(conn, args: argparse.Namespace) -> int:
    _print_counts("scan-dbs", scan_results(conn, args.results_root, rescan=args.rescan))
    return 0


def cmd_discover(conn, args: argparse.Namespace) -> int:
    extensions = set(discover_mod.DEFAULT_EXTENSIONS)
    if args.include_txt:
        extensions.add(".txt")
    max_mb = None if args.max_mb <= 0 else args.max_mb
    counts = discover_mod.discover(conn, args.homes_root, extensions, max_mb)
    _print_counts("discover", counts)
    candidates = conn.execute(
        "SELECT count(*) FROM meta_file WHERE status = 'candidate'"
    ).fetchone()[0]
    print(f"sheets awaiting ingest: {candidates}")
    return 0


def cmd_ingest(conn, args: argparse.Namespace) -> int:
    if args.files:
        counts = discover_mod.ingest_files(conn, args.files, home=args.home, force=args.force)
    else:
        counts = discover_mod.ingest_candidates(conn)
    _print_counts("ingest", counts)
    return 0


def cmd_match(conn, args: argparse.Namespace) -> int:
    _print_counts("match", match_mod.match_all(conn, args.time_tolerance))
    return 0


def cmd_report(conn, args: argparse.Namespace) -> int:
    print(report_mod.text_summary(conn))
    if args.out_dir:
        for path in report_mod.write_report(conn, args.out_dir):
            print(f"wrote {path}")
    return 0


def cmd_query(conn, args: argparse.Namespace) -> int:
    try:
        result = report_mod.query(conn, args.expression)
    except Exception as exc:  # noqa: BLE001 - surface any pandas query error to the user
        print(f"query failed: {exc}", file=sys.stderr)
        return 2
    n_dbs = result["path"].nunique()
    if args.ethoscopy:
        result = report_mod.to_ethoscopy(result)
    if args.out:
        result.to_csv(args.out, index=False)
        print(f"{len(result)} tubes in {n_dbs} databases -> {args.out}")
    else:
        result.to_csv(sys.stdout, index=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the ``ethocensus`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="ethocensus", description="Census of ethoscope databases and their metadata."
    )
    parser.add_argument("--census", required=True, help="census SQLite file (created if absent)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log progress")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan-dbs", help="index the ethoscope results tree")
    p.add_argument("results_root")
    p.add_argument("--rescan", action="store_true", help="re-read unchanged databases")
    p.set_defaults(func=cmd_scan_dbs)

    p = sub.add_parser("discover", help="find metadata files under a homes root")
    p.add_argument("homes_root")
    p.add_argument(
        "--max-mb",
        type=float,
        default=discover_mod.DEFAULT_MAX_MB,
        help="skip larger files (0: no limit)",
    )
    p.add_argument("--include-txt", action="store_true", help="also consider .txt files")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("ingest", help="parse discovered (or named) metadata files")
    p.add_argument("files", nargs="*", help="ingest these files regardless of score")
    p.add_argument("--home", help="provenance label for named files")
    p.add_argument("--force", action="store_true", help="re-read even if unchanged")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("match", help="match metadata rows to databases")
    p.add_argument(
        "--time-tolerance",
        type=float,
        default=match_mod.DEFAULT_TIME_TOLERANCE_MIN,
        help="start-time tolerance in minutes",
    )
    p.set_defaults(func=cmd_match)

    p = sub.add_parser("report", help="summarise the census")
    p.add_argument("--out-dir", help="write CSV tables here")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("query", help="select annotated tubes")
    p.add_argument("expression", help="pandas query, e.g. \"genotype in ['CS', 'w1118']\"")
    p.add_argument("--out", help="CSV output file (default: stdout)")
    p.add_argument(
        "--ethoscopy",
        action="store_true",
        help="write only the columns ethoscopy's link_meta_index needs, with no empty cells",
    )
    p.set_defaults(func=cmd_query)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``ethocensus`` console script."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    try:
        conn = store.connect(args.census)
    except store.SchemaVersionError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
