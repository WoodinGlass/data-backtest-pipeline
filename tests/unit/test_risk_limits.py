"""Unit tests for risk.limits. See ADR 0013 §3."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from risk.config import RiskSettings
from risk.limits import apply_all_limits, cap_position_limits


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

def _stop_loss_fixture():
    """Single ticker, price drops -10% on day 3 -> stop-out."""
    dates = pd.date_range("2024-01-02", periods=6, freq="B").date
    prices = pd.DataFrame({
        "ticker": ["AAA"] * 6,
        "trade_date": list(dates),
        "close": [100.0, 95.0, 90.0, 92.0, 95.0, 100.0],
    })
    weights = pd.DataFrame({
        "ticker": ["AAA"] * 6,
        "trade_date": list(dates),
        "weight": [0.03] * 6,
    })
    return weights, prices


def _dd_fixture():
    """Single ticker, weight=1.0, price drops -20% over 5 days."""
    dates = pd.date_range("2024-01-02", periods=30, freq="B").date
    prices = pd.DataFrame({
        "ticker": ["AAA"] * 30,
        "trade_date": list(dates),
        "close": [100.0] * 10 + [95.0, 90.0, 85.0, 82.0, 80.0] + [80.0] * 15,
    })
    weights = pd.DataFrame({
        "ticker": ["AAA"] * 30,
        "trade_date": list(dates),
        "weight": [1.0] * 30,
    })
    return weights, prices


# ---------------------------------------------------------------------
# Stop loss
# ---------------------------------------------------------------------

class TestStopLoss:
    def test_triggers_at_threshold(self) -> None:
        w, p = _stop_loss_fixture()
        out = apply_all_limits(w, p, RiskSettings())
        # Day 3: price 90 vs entry 100 -> -10% <= -8% -> stop
        assert out.loc[2, "weight"] == 0.0
        assert bool(out.loc[2, "stop_out"]) is True

    def test_not_triggered_when_shallow(self) -> None:
        w, p = _stop_loss_fixture()
        out = apply_all_limits(w, p, RiskSettings())
        # Day 2: price 95 -> -5% > -8% -> no stop
        assert out.loc[1, "weight"] == 0.03
        assert bool(out.loc[1, "stop_out"]) is False

    def test_cooldown_blocks_reentry(self) -> None:
        w, p = _stop_loss_fixture()
        out = apply_all_limits(w, p, RiskSettings())
        # Days 4,5,6 should be 0 due to cooldown
        assert (out.loc[3:5, "weight"] == 0.0).all()

    def test_disabled_stop_loss(self) -> None:
        w, p = _stop_loss_fixture()
        rs = RiskSettings(stop_loss_pct=-0.99)
        out = apply_all_limits(w, p, rs)
        # No stop-out possible; weight stays 0.03 (capped, no DD hit)
        assert (out["weight"] == 0.03).all()
        assert not out["stop_out"].any()


# ---------------------------------------------------------------------
# Drawdown derisk / halt
# ---------------------------------------------------------------------

class TestDrawdownDerisk:
    def test_derisk_fires_and_prevents_halt(self) -> None:
        w, p = _dd_fixture()
        rs = RiskSettings(
            kelly_cap=1.0, stop_loss_pct=-0.99, dd_derisk_factor=0.5,
        )
        out = apply_all_limits(w, p, rs)
        derisk = out[out["dd_state"] == "derisk"]
        halt = out[out["dd_state"] == "halt"]
        assert len(derisk) > 0
        assert len(halt) == 0  # derisk prevents reaching halt
        assert (derisk["weight"] == 0.5).all()

    def test_derisk_first_fires_at_minus_10pct(self) -> None:
        w, p = _dd_fixture()
        rs = RiskSettings(
            kelly_cap=1.0, stop_loss_pct=-0.99, dd_derisk_factor=0.5,
        )
        out = apply_all_limits(w, p, rs)
        first = out[out["dd_state"] == "derisk"]["trade_date"].min()
        # Price 90 -> DD=-10% -> fires 2024-01-17
        assert first == pd.to_datetime("2024-01-17").date()


class TestDrawdownHalt:
    def test_halt_fires_when_derisk_disabled(self) -> None:
        w, p = _dd_fixture()
        rs = RiskSettings(
            kelly_cap=1.0, stop_loss_pct=-0.99, dd_derisk_factor=1.0,
        )
        out = apply_all_limits(w, p, rs)
        halt = out[out["dd_state"] == "halt"]
        assert len(halt) > 0
        # First halt at price 80 -> DD=-20%
        first = halt["trade_date"].min()
        assert first == pd.to_datetime("2024-01-22").date()

    def test_post_halt_all_weights_zero(self) -> None:
        w, p = _dd_fixture()
        rs = RiskSettings(
            kelly_cap=1.0, stop_loss_pct=-0.99, dd_derisk_factor=1.0,
        )
        out = apply_all_limits(w, p, rs)
        halt_start = out[out["dd_state"] == "halt"]["trade_date"].min()
        after = out[out["trade_date"] >= halt_start]
        assert (after["weight"] == 0.0).all()


# ---------------------------------------------------------------------
# cap_position_limits
# ---------------------------------------------------------------------

class TestCapPositionLimits:
    def test_clips_above_cap(self) -> None:
        w = pd.DataFrame({
            "ticker": ["A", "B", "C"],
            "trade_date": [pd.to_datetime("2024-01-02").date()] * 3,
            "weight": [0.01, 0.10, 0.03],
        })
        capped = cap_position_limits(w, RiskSettings())
        assert capped.loc[1, "weight"] == 0.05
        assert capped.loc[0, "weight"] == 0.01
        assert capped.loc[2, "weight"] == 0.03

    def test_clips_negative_when_long_only(self) -> None:
        w = pd.DataFrame({
            "ticker": ["A"],
            "trade_date": [pd.to_datetime("2024-01-02").date()],
            "weight": [-0.05],
        })
        capped = cap_position_limits(w, RiskSettings())
        assert capped.loc[0, "weight"] == 0.0


# ---------------------------------------------------------------------
# Purity & schema
# ---------------------------------------------------------------------

class TestPurityAndSchema:
    def test_input_not_mutated(self) -> None:
        w, p = _stop_loss_fixture()
        w_before = w.copy()
        p_before = p.copy()
        _ = apply_all_limits(w, p, RiskSettings())
        pd.testing.assert_frame_equal(w, w_before)
        pd.testing.assert_frame_equal(p, p_before)

    def test_returns_expected_columns(self) -> None:
        w, p = _stop_loss_fixture()
        out = apply_all_limits(w, p, RiskSettings())
        assert set(out.columns) == {
            "ticker", "trade_date",
            "weight_before_limits", "weight",
            "stop_out", "dd_state",
        }

    def test_row_order_matches_input(self) -> None:
        w, p = _stop_loss_fixture()
        # Shuffle
        shuffled = w.iloc[[3, 1, 5, 0, 2, 4]].copy()
        out = apply_all_limits(shuffled, p, RiskSettings())
        assert list(out["ticker"]) == list(shuffled["ticker"])
        assert list(out["trade_date"]) == list(shuffled["trade_date"])

    def test_deterministic(self) -> None:
        w, p = _stop_loss_fixture()
        rs = RiskSettings()
        a = apply_all_limits(w, p, rs)
        b = apply_all_limits(w, p, rs)
        pd.testing.assert_frame_equal(a, b)

    def test_weight_before_limits_preserved(self) -> None:
        w, p = _stop_loss_fixture()
        out = apply_all_limits(w, p, RiskSettings())
        assert (out["weight_before_limits"] == 0.03).all()

    def test_stop_out_is_boolean(self) -> None:
        w, p = _stop_loss_fixture()
        out = apply_all_limits(w, p, RiskSettings())
        assert out["stop_out"].dtype == bool

    def test_dd_state_is_string(self) -> None:
        w, p = _stop_loss_fixture()
        out = apply_all_limits(w, p, RiskSettings())
        assert set(out["dd_state"].unique()) <= {"normal", "derisk", "halt"}


# ---------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------

class TestErrors:
    def test_missing_weight_column(self) -> None:
        w, p = _stop_loss_fixture()
        with pytest.raises(ValueError, match="missing columns"):
            apply_all_limits(w.drop(columns=["weight"]), p, RiskSettings())

    def test_missing_close_column(self) -> None:
        w, p = _stop_loss_fixture()
        with pytest.raises(ValueError, match="missing columns"):
            apply_all_limits(w, p.drop(columns=["close"]), RiskSettings())

    def test_non_positive_close(self) -> None:
        w, p = _stop_loss_fixture()
        bad = p.copy()
        bad.loc[0, "close"] = -1.0
        with pytest.raises(ValueError, match="positive"):
            apply_all_limits(w, bad, RiskSettings())

    def test_empty_input(self) -> None:
        w, p = _stop_loss_fixture()
        empty = w.iloc[0:0].copy()
        out = apply_all_limits(empty, p, RiskSettings())
        assert len(out) == 0
        assert "weight" in out.columns
        assert "dd_state" in out.columns
