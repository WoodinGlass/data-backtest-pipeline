"""CLI for SEC fundamental ingestion.

Usage:
    dbp-ingest-sec                              # all universe tickers
    dbp-ingest-sec --tickers AAPL MSFT          # subset
    dbp-ingest-sec --only-missing               # resume after partial run
    dbp-ingest-sec --list-ciks                  # show ticker -> CIK mapping

Also runnable as a module:

    python -m ingestion.sec.cli --help
"""

from __future__ import annotations

import argparse
import sys

from ingestion.config import get_settings, load_universe
from ingestion.logging import configure_logging, get_logger
from ingestion.sec.client import SecClient, SecFetchError
from ingestion.sec.pipeline import ingest_sec_universe

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. Exposed separately for tests."""
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="dbp-ingest-sec",
        description="Fetch SEC EDGAR fundamentals into the raw layer.",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Subset of tickers (default: config/universe.txt)",
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help=(
            "Skip tickers that already have at least one snapshot in "
            "the manifest. Use to resume after a partial run."
        ),
    )
    parser.add_argument(
        "--list-ciks",
        action="store_true",
        help="Print the ticker -> CIK mapping and exit (no fetch).",
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
    log = get_logger("sec.cli")

    settings = get_settings()
    tickers = (
        [t.upper() for t in args.tickers] if args.tickers else load_universe(settings.universe_file)
    )

    if args.list_ciks:
        client = SecClient()
        try:
            print(f"{len(tickers)} tickers, CIK mapping:")
            for t in tickers:
                cik = client.get_cik(t)
                cik_str = f"{cik}" if cik is not None else "(not found)"
                print(f"  {t:8s} → {cik_str}")
        except SecFetchError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        return 0

    log.info("cli_start", n_tickers=len(tickers), only_missing=args.only_missing)

    summary = ingest_sec_universe(
        tickers=tickers,
        only_missing=args.only_missing,
    )

    print(
        f"[dbp-ingest-sec] "
        f"written={len(summary.succeeded)} "
        f"skipped={len(summary.skipped)} "
        f"failed={len(summary.failed)} "
        f"facts={summary.total_facts} "
        f"correlation_id={summary.correlation_id}"
    )
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
