"""CLI for macro ingestion.

Usage:
    dbp-ingest-macro                            # all ~150 series
    dbp-ingest-macro --series FEDFUNDS DGS10    # subset
    dbp-ingest-macro --start 2023-01-01
    dbp-ingest-macro --category rates
    dbp-ingest-macro --list                     # list registry and exit

Also runnable as a module:

    python -m ingestion.macro.cli --help
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from ingestion.config import get_settings
from ingestion.logging import configure_logging, get_logger
from ingestion.macro.config import (
    get_macro_settings,
    load_macro_registry,
)
from ingestion.macro.pipeline import ingest_macro_universe

__all__ = ["build_parser", "main"]


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Not a valid ISO date (YYYY-MM-DD): {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. Exposed separately for tests."""
    macro_settings = get_macro_settings()
    settings = get_settings()  # for logging defaults
    parser = argparse.ArgumentParser(
        prog="dbp-ingest-macro",
        description="Fetch vintage-aware macro series into the raw layer.",
    )
    parser.add_argument(
        "--start",
        type=_iso_date,
        default=macro_settings.macro_history_start,
        help=f"Earliest observation date (default: {macro_settings.macro_history_start})",
    )
    parser.add_argument(
        "--end",
        type=_iso_date,
        default=None,
        help="Latest observation date (default: today)",
    )
    parser.add_argument(
        "--series",
        nargs="+",
        default=None,
        help="Subset of series_ids to ingest (default: full registry)",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Only ingest series in this category (e.g. rates, inflation)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the registry and exit (no fetch).",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=settings.log_level,
    )
    parser.add_argument(
        "--log-format",
        choices=["json", "console"],
        default=settings.log_format,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(level=args.log_level, fmt=args.log_format)
    log = get_logger("macro.cli")

    registry = load_macro_registry()

    if args.list:
        print(f"{len(registry)} series in registry:")
        by_cat: dict[str, list[str]] = {}
        for s in registry:
            by_cat.setdefault(s.category, []).append(s.series_id)
        for cat in sorted(by_cat):
            ids = by_cat[cat]
            print(f"  {cat:20s} {len(ids):3d}  ({', '.join(ids[:3])}...)")
        return 0

    # Filter by category and/or explicit ids
    if args.category:
        registry = [s for s in registry if s.category == args.category]
        if not registry:
            print(f"No series in category {args.category!r}", file=sys.stderr)
            return 2

    if args.series:
        wanted = {s.upper() for s in args.series}
        registry = [s for s in registry if s.series_id in wanted]
        if not registry:
            print(f"No matching series in registry: {sorted(wanted)}", file=sys.stderr)
            return 2

    log.info("cli_start", n_series=len(registry), start=str(args.start))

    summary = ingest_macro_universe(
        start=args.start,
        end=args.end,
        series=registry,
    )

    print(
        f"[dbp-ingest-macro] "
        f"written_series={len(summary.succeeded)} "
        f"skipped_series={len(summary.skipped)} "
        f"failed_series={len(summary.failed)} "
        f"vintages_written={summary.total_vintages_written} "
        f"observations={summary.total_observations} "
        f"correlation_id={summary.correlation_id}"
    )
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
