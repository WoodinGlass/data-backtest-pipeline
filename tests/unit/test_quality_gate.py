"""Unit tests for quality/gate.py — no external services."""

from datetime import UTC, datetime

import pandas as pd
import pytest

from quality.gate import (
    GateReport,
    LayerResult,
    _validate,
    run_gate,
)
from quality.schemas import RawPricesSchema


# ─── helpers ────────────────────────────────────────────────
def _raw_df(n: int = 3) -> pd.DataFrame:
    from datetime import date

    return pd.DataFrame(
        {
            "ticker": ["AAPL"] * n,
            "date": pd.to_datetime([date(2024, 1, 2 + i) for i in range(n)]),
            "open": [100.0 + i for i in range(n)],
            "high": [102.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [101.0 + i for i in range(n)],
            "adj_close": [101.0 + i for i in range(n)],
            "volume": [1000 * (i + 1) for i in range(n)],
        }
    )


# ═══════════════════════════════════════════════════════════
# LayerResult
# ═══════════════════════════════════════════════════════════
def test_layer_result_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    r = LayerResult(layer="x", status="pass")
    with pytest.raises(FrozenInstanceError):
        r.layer = "y"  # type: ignore[misc]


def test_layer_result_defaults() -> None:
    r = LayerResult(layer="x", status="pass")
    assert r.n_rows == 0
    assert r.n_columns == 0
    assert r.error is None
    assert r.detail == {}


# ═══════════════════════════════════════════════════════════
# GateReport
# ═══════════════════════════════════════════════════════════
def _make_report(results: list[LayerResult]) -> GateReport:
    now = datetime.now(tz=UTC)
    return GateReport(started_at=now, finished_at=now, results=results)


def test_gate_report_exit_code_zero_when_all_pass() -> None:
    r = _make_report([LayerResult("a", "pass"), LayerResult("b", "pass")])
    assert r.exit_code == 0
    assert len(r.passed) == 2
    assert r.failed == []


def test_gate_report_exit_code_one_on_any_failure() -> None:
    r = _make_report(
        [
            LayerResult("a", "pass"),
            LayerResult("b", "fail", error="boom"),
        ]
    )
    assert r.exit_code == 1
    assert len(r.failed) == 1


def test_gate_report_to_dict_has_expected_keys() -> None:
    r = _make_report([LayerResult("a", "pass")])
    d = r.to_dict()
    assert set(d.keys()) >= {
        "started_at",
        "finished_at",
        "duration_seconds",
        "n_pass",
        "n_fail",
        "results",
    }
    assert d["n_pass"] == 1
    assert d["n_fail"] == 0


def test_gate_report_to_json_is_valid() -> None:
    import json

    r = _make_report([LayerResult("a", "pass")])
    parsed = json.loads(r.to_json())
    assert parsed["n_pass"] == 1


# ═══════════════════════════════════════════════════════════
# _validate
# ═══════════════════════════════════════════════════════════
def test_validate_pass_on_valid_frame() -> None:
    result = _validate(layer="raw:AAPL", df=_raw_df(), schema=RawPricesSchema)
    assert result.status == "pass"
    assert result.n_rows == 3
    assert result.error is None


def test_validate_fail_on_invalid_frame() -> None:
    df = _raw_df()
    df.loc[0, "low"] = 999.0
    result = _validate(layer="raw:AAPL", df=df, schema=RawPricesSchema)
    assert result.status == "fail"
    assert result.error is not None
    assert "ohlc_invariants" in result.error or "Check" in result.error


# ═══════════════════════════════════════════════════════════
# run_gate — hermetic (skip all layers)
# ═══════════════════════════════════════════════════════════
ALL_LAYERS = frozenset(
    {
        "raw",
        "staging",
        "marts",
        "sec",
        "stg_macro",
        "int_macro",
        "stg_sec",
        "int_fundamentals",
    }
)


def test_run_gate_with_all_layers_skipped() -> None:
    report = run_gate(skip=set(ALL_LAYERS))
    assert report.exit_code == 0
    assert report.results == []
    assert report.duration_seconds >= 0


def test_run_gate_with_missing_ticker_fails() -> None:
    report = run_gate(
        tickers=["NOTAREALTICKER999"],
        skip={"staging", "marts", "sec", "stg_macro", "int_macro", "stg_sec", "int_fundamentals"},
    )
    assert report.exit_code == 1
    assert len(report.failed) == 1
    assert "NOTAREALTICKER999" in report.failed[0].layer
