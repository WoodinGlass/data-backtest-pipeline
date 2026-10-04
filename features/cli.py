"""CLI for feature engineering.

Usage:
    dbp-features            # build features for the current version
    dbp-features --force    # overwrite even if content hash unchanged
    dbp-features --info     # show file info without building
"""

from __future__ import annotations

import argparse
import sys

from features.assembler import build_features, load_features
from features.config import FEATURE_VERSION, get_feature_settings
from ingestion.logging import configure_logging, get_logger

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbp-features",
        description="Build the point-in-time feature table.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite even if content hash unchanged.",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Print feature file info and exit.",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(level=args.log_level, fmt="json")
    log = get_logger("features.cli")

    settings = get_feature_settings()

    if args.info:
        try:
            df = load_features(settings=settings)
        except FileNotFoundError as exc:
            print(f"  {exc}", file=sys.stderr)
            return 1
        print(f"  version  = {FEATURE_VERSION}")
        print(f"  path     = {settings.features_path}")
        print(f"  shape    = {df.shape}")
        print(f"  columns  = {list(df.columns)}")
        return 0

    log.info("features_start", version=FEATURE_VERSION)
    try:
        path = build_features(settings=settings, force=args.force)
    except FileNotFoundError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"  ✅ Features written: {path}")
    log.info("features_end", path=str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
