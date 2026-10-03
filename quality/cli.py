"""Command-line entry point for the data quality gate.

Usage:
    dbp-quality                            # validate every layer
    dbp-quality --skip raw                 # staging + marts only
    dbp-quality --tickers AAPL MSFT        # subset of raw tickers
    dbp-quality --tickers-from-raw         # auto-discover from raw dir
    dbp-quality --json report.json         # write JSON report
    dbp-quality --quiet                    # suppress progress lines

Also runnable as a module:

    python -m quality.cli --help
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ingestion.config import get_settings, load_universe
from ingestion.logging import configure_logging, get_logger
from quality.gate import (
    GateReport,
    discover_tickers_from_raw,
    run_gate,
)

__all__ = ["build_parser", "format_summary", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser. Exposed separately for tests."""
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="dbp-quality",
        description="Validate raw / staging / marts layers against Pandera schemas.",
    )
    parser.add_argument(
        "--warehouse",
        type=Path,
        default=settings.duckdb_path,
        help=f"DuckDB warehouse path (default: {settings.duckdb_path})",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Override raw tickers (default: config/universe.txt)",
    )
    parser.add_argument(
        "--tickers-from-raw",
        action="store_true",
        help=(
            "Auto-detect tickers by scanning the raw data directory. "
            "Useful in CI, where the raw layer is a small fixture."
        ),
    )
    parser.add_argument(
        "--skip",
        nargs="+",
        default=[],
        choices=[
            "raw",
            "staging",
            "marts",
            "sec",
            "stg_macro",
            "int_macro",
            "stg_sec",
            "int_fundamentals",
        ],
        help="Skip specific layers.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Write the full gate report as JSON to this path.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-layer progress lines.",
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


def format_summary(report: GateReport) -> str:
    """Return a compact human-readable summary."""
    lines = [
        "",
        "─" * 66,
        f"  Gate summary  |  {len(report.passed)} pass  |  "
        f"{len(report.failed)} fail  |  {report.duration_seconds:.2f}s",
        "─" * 66,
    ]
    for r in report.failed:
        lines.append(f"  FAIL  {r.layer}")
        if r.error:
            first_line = r.error.splitlines()[0][:120]
            lines.append(f"        {first_line}")
    if not report.failed:
        lines.append("  All layers valid.")
    lines.append("─" * 66)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(level=args.log_level, fmt=args.log_format)
    log = get_logger("quality.cli")

    # Resolve tickers: explicit > auto-discover > universe file
    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
        source = "explicit"
    elif args.tickers_from_raw:
        tickers = discover_tickers_from_raw()
        source = "raw_dir"
    else:
        tickers = load_universe()
        source = "universe_file"

    if not tickers:
        print(
            "ERROR: no tickers to validate. Check --tickers, "
            "--tickers-from-raw, or config/universe.txt.",
            file=sys.stderr,
        )
        return 2

    skip = set(args.skip or [])

    log.info(
        "quality_start",
        n_tickers=len(tickers),
        ticker_source=source,
        skip=sorted(skip),
        warehouse=str(args.warehouse),
    )

    def progress(msg: str) -> None:
        if not args.quiet:
            print(msg)

    report = run_gate(
        tickers=tickers,
        warehouse_path=args.warehouse,
        skip=skip,
        log=progress,
    )

    print(format_summary(report))

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(report.to_json())
        print(f"  JSON report: {args.json}")

    log.info(
        "quality_end",
        n_pass=len(report.passed),
        n_fail=len(report.failed),
        exit_code=report.exit_code,
    )
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
