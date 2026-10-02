"""Unit tests for ingestion/macro/cli.py."""

from datetime import date

import pytest

from ingestion.macro.cli import build_parser


def test_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.series is None
    assert args.category is None
    assert args.end is None
    assert args.list is False
    assert isinstance(args.start, date)


def test_parser_series_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["--series", "FEDFUNDS", "DGS10"])
    assert args.series == ["FEDFUNDS", "DGS10"]


def test_parser_category() -> None:
    parser = build_parser()
    args = parser.parse_args(["--category", "rates"])
    assert args.category == "rates"


def test_parser_dates() -> None:
    parser = build_parser()
    args = parser.parse_args(["--start", "2023-01-01", "--end", "2024-12-31"])
    assert args.start == date(2023, 1, 1)
    assert args.end == date(2024, 12, 31)


def test_parser_rejects_bad_date() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--start", "not-a-date"])


def test_parser_list_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(["--list"])
    assert args.list is True
