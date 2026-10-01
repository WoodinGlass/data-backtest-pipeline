"""Unit tests for ingestion/cli.py — no network, no real ingestion."""

from datetime import date

import pytest

from ingestion.cli import build_parser


def test_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.tickers is None
    assert args.end is None
    # Start defaults to settings, which we do not pin here; just check type
    assert isinstance(args.start, date)


def test_parser_tickers_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["--tickers", "AAPL", "MSFT"])
    assert args.tickers == ["AAPL", "MSFT"]


def test_parser_parses_dates() -> None:
    parser = build_parser()
    args = parser.parse_args(["--start", "2024-01-01", "--end", "2024-02-01"])
    assert args.start == date(2024, 1, 1)
    assert args.end == date(2024, 2, 1)


def test_parser_rejects_bad_date() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--start", "not-a-date"])


def test_parser_log_options() -> None:
    parser = build_parser()
    args = parser.parse_args(["--log-level", "DEBUG", "--log-format", "console"])
    assert args.log_level == "DEBUG"
    assert args.log_format == "console"
