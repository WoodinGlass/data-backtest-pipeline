"""Unit tests for ingestion/sec/cli.py."""

from ingestion.sec.cli import build_parser


def test_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.tickers is None
    assert args.only_missing is False
    assert args.list_ciks is False


def test_parser_tickers_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["--tickers", "AAPL", "MSFT"])
    assert args.tickers == ["AAPL", "MSFT"]


def test_parser_only_missing() -> None:
    parser = build_parser()
    args = parser.parse_args(["--only-missing"])
    assert args.only_missing is True


def test_parser_list_ciks() -> None:
    parser = build_parser()
    args = parser.parse_args(["--list-ciks"])
    assert args.list_ciks is True


def test_parser_log_options() -> None:
    parser = build_parser()
    args = parser.parse_args(["--log-level", "DEBUG", "--log-format", "console"])
    assert args.log_level == "DEBUG"
    assert args.log_format == "console"
