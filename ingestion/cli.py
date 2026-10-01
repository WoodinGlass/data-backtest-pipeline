"""Command-line entry point for ingestion.

Wired into ``pyproject.toml`` as ``dbp-ingest``:

    dbp-ingest --help
    dbp-ingest
    dbp-ingest --start 2024-01-01 --end 2024-02-01 --tickers AAPL MSFT

Also runnable as a module:

    python -m ingestion.cli --help
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from ingestion.config import get_settings, load_universe
from ingestion.logging import configure_logging, get_logger
from ingestion.pipeline import ingest_universe

__all__ = ["build_parser", "main"]


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Not a valid ISO date (YYYY-MM-DD): {value!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. Exposed separately for tests."""
    settings = get_settings()

    parser = argparse.ArgumentParser(
        prog="dbp-ingest",
        description="Fetch daily OHLCV bars into the immutable raw layer.",
    )
    parser.add_argument(
        "--start",
        type=_iso_date,
        default=settings.price_history_start,
        help=f"Inclusive start date (default: {settings.price_history_start})",
    )
    parser.add_argument(
        "--end",
        type=_iso_date,
        default=None,
        help="Exclusive end date (default: tomorrow UTC, so today is included)",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Override the universe (default: config/universe.txt)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=settings.log_level,
        help=f"Log level (default: {settings.log_level})",
    )
    parser.add_argument(
        "--log-format",
        choices=["json", "console"],
        default=settings.log_format,
        help=f"Log format (default: {settings.log_format})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(level=args.log_level, fmt=args.log_format)
    log = get_logger("cli")

    settings = get_settings()
    tickers = (
        [t.upper() for t in args.tickers] if args.tickers else load_universe(settings.universe_file)
    )

    log.info(
        "cli_start",
        n_tickers=len(tickers),
        start=str(args.start),
        end=str(args.end) if args.end else "auto",
    )

    summary = ingest_universe(
        start=args.start,
        end=args.end,
        tickers=tickers,
        settings=settings,
    )

    # Human-readable final line (even in JSON mode)
    print(
        f"[dbp-ingest] written={len(summary.succeeded)} "
        f"skipped={len(summary.skipped)} failed={len(summary.failed)} "
        f"rows={summary.total_rows} correlation_id={summary.correlation_id}"
    )
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
