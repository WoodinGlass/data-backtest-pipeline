"""Unit tests for quality/cli.py — no external services."""

from datetime import UTC, datetime

import pytest

from quality.cli import build_parser, format_summary
from quality.gate import GateReport, LayerResult


# ═══════════════════════════════════════════════════════════
# build_parser
# ═══════════════════════════════════════════════════════════
def test_parser_defaults() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.tickers is None
    assert args.skip == []
    assert args.json is None
    assert args.quiet is False


def test_parser_tickers_override() -> None:
    parser = build_parser()
    args = parser.parse_args(["--tickers", "AAPL", "MSFT"])
    assert args.tickers == ["AAPL", "MSFT"]


def test_parser_skip_choices() -> None:
    parser = build_parser()
    args = parser.parse_args(["--skip", "raw", "marts"])
    assert set(args.skip) == {"raw", "marts"}


def test_parser_rejects_invalid_skip_choice() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--skip", "not-a-layer"])


def test_parser_quiet_flag() -> None:
    parser = build_parser()
    args = parser.parse_args(["--quiet"])
    assert args.quiet is True


# ═══════════════════════════════════════════════════════════
# format_summary
# ═══════════════════════════════════════════════════════════
def _report(results: list[LayerResult]) -> GateReport:
    now = datetime.now(tz=UTC)
    return GateReport(started_at=now, finished_at=now, results=results)


def test_format_summary_all_pass() -> None:
    r = _report([LayerResult("raw:AAPL", "pass", n_rows=21)])
    s = format_summary(r)
    assert "1 pass" in s
    assert "0 fail" in s
    assert "All layers valid" in s


def test_format_summary_with_failure() -> None:
    r = _report(
        [
            LayerResult("raw:AAPL", "pass"),
            LayerResult("raw:FAKE", "fail", error="load failed: FileNotFoundError: x"),
        ]
    )
    s = format_summary(r)
    assert "1 pass" in s
    assert "1 fail" in s
    assert "FAIL" in s
    assert "raw:FAKE" in s
    assert "load failed" in s
