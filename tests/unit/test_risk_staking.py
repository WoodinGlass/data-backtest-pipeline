"""Unit tests for risk.staking. See ADR 0013 §1, §2, §8."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from risk.config import RiskSettings
from risk.staking import (
    _STAKING_REGISTRY,
    available_staking_methods,
    compute_weights,
    register_staking,
)


def _signals(probs, vols=None, date="2024-01-02"):
    """Build a minimal signals frame for staking tests."""
    n = len(probs)
    return pd.DataFrame(
        {
            "ticker": [f"T{i}" for i in range(n)],
            "trade_date": pd.to_datetime([date] * n).date,
            "prob": list(probs),
            "realized_vol": list(vols) if vols is not None else [0.20] * n,
        }
    )


class TestRegistry:
    def test_expected_methods_registered(self) -> None:
        methods = available_staking_methods()
        assert set(methods) == {
            "kelly",
            "fixed_fractional",
            "equal_weight",
            "vol_target",
        }

    def test_register_new_method(self) -> None:
        @register_staking("test_custom_rule")
        def _custom(signals, settings):
            return pd.Series(0.0, index=signals.index)

        try:
            assert "test_custom_rule" in available_staking_methods()
        finally:
            _STAKING_REGISTRY.pop("test_custom_rule", None)

    def test_register_duplicate_raises(self) -> None:
        with pytest.raises(ValueError):

            @register_staking("kelly")
            def _dup(signals, settings):
                return pd.Series(0.0, index=signals.index)


class TestKelly:
    def test_hand_computed_values(self) -> None:
        sig = _signals([0.50, 0.55, 0.70, 0.90])
        w = compute_weights(sig, RiskSettings(staking_method="kelly"))
        # p=0.50 -> f*=0.00 -> 0.0
        # p=0.55 -> f*=0.10 -> 0.025
        # p=0.70 -> f*=0.40 -> 0.10 -> cap 0.05
        # p=0.90 -> f*=0.80 -> 0.20 -> cap 0.05
        np.testing.assert_allclose(w.values, [0.0, 0.025, 0.05, 0.05])

    def test_negative_kelly_clipped_to_zero(self) -> None:
        sig = _signals([0.30, 0.45])
        w = compute_weights(sig, RiskSettings(staking_method="kelly"))
        assert (w == 0.0).all()

    def test_custom_kelly_fraction(self) -> None:
        sig = _signals([0.70])  # f*=0.40
        w = compute_weights(
            sig,
            RiskSettings(staking_method="kelly", kelly_fraction=0.5),
        )
        # 0.5 * 0.40 = 0.20 -> cap 0.05
        assert w.iloc[0] == 0.05


class TestFixedFractional:
    def test_uniform_size(self) -> None:
        sig = _signals([0.50, 0.90, 0.99])
        w = compute_weights(sig, RiskSettings(staking_method="fixed_fractional"))
        assert (w == 0.03).all()

    def test_capped_at_kelly_cap(self) -> None:
        sig = _signals([0.50])
        w = compute_weights(
            sig,
            RiskSettings(
                staking_method="fixed_fractional",
                fixed_fractional=0.50,
            ),
        )
        assert w.iloc[0] == 0.05


class TestEqualWeight:
    def test_1_over_n_per_date(self) -> None:
        # Day 1: 4 names -> 0.25 -> capped to 0.05
        # Day 2: 2 names -> 0.50 -> capped to 0.05
        sig = pd.DataFrame(
            {
                "ticker": list("ABCD") + list("XY"),
                "trade_date": (
                    [pd.to_datetime("2024-01-02").date()] * 4
                    + [pd.to_datetime("2024-01-03").date()] * 2
                ),
                "prob": [0.5] * 6,
                "realized_vol": [0.20] * 6,
            }
        )
        w = compute_weights(sig, RiskSettings(staking_method="equal_weight"))
        assert (w == 0.05).all()

    def test_below_cap_when_many_names(self) -> None:
        # 25 names -> 1/25 = 0.04 (< cap 0.05)
        sig = _signals([0.5] * 25)
        w = compute_weights(sig, RiskSettings(staking_method="equal_weight"))
        assert np.isclose(w.iloc[0], 0.04)


class TestVolTarget:
    def test_hand_computed_values(self) -> None:
        sig = _signals(
            [0.7] * 4,
            vols=[0.20, 0.15, 0.30, 0.10],
        )
        w = compute_weights(sig, RiskSettings(staking_method="vol_target"))
        # target=0.10, cap=0.05
        # 0.20 -> 0.10/0.20 = 0.5  -> 0.025
        # 0.15 -> 0.667             -> 0.03333
        # 0.30 -> 0.333             -> 0.01667
        # 0.10 -> 1.0               -> 0.05
        np.testing.assert_allclose(
            w.values,
            [0.025, 0.10 / 0.15 * 0.05, 0.10 / 0.30 * 0.05, 0.05],
        )

    def test_missing_vol_falls_back_to_full_cap(self) -> None:
        sig = _signals([0.7] * 2, vols=[np.nan, np.nan])
        w = compute_weights(sig, RiskSettings(staking_method="vol_target"))
        assert (w == 0.05).all()

    def test_zero_vol_falls_back_to_full_cap(self) -> None:
        sig = _signals([0.7] * 2, vols=[0.0, 0.0])
        w = compute_weights(sig, RiskSettings(staking_method="vol_target"))
        assert (w == 0.05).all()

    def test_requires_realized_vol_column(self) -> None:
        sig = _signals([0.7]).drop(columns=["realized_vol"])
        with pytest.raises(ValueError, match="realized_vol"):
            compute_weights(sig, RiskSettings(staking_method="vol_target"))


class TestPurity:
    def test_input_not_mutated(self) -> None:
        sig = _signals([0.6, 0.7])
        before = sig.copy()
        _ = compute_weights(sig, RiskSettings())
        pd.testing.assert_frame_equal(sig, before)

    def test_deterministic_across_calls(self) -> None:
        sig = _signals([0.6, 0.7])
        rs = RiskSettings()
        pd.testing.assert_series_equal(
            compute_weights(sig, rs),
            compute_weights(sig, rs),
        )


class TestErrors:
    def test_missing_prob_column(self) -> None:
        sig = _signals([0.6]).drop(columns=["prob"])
        with pytest.raises(ValueError, match="prob"):
            compute_weights(sig, RiskSettings())

    def test_missing_ticker_column(self) -> None:
        sig = _signals([0.6]).drop(columns=["ticker"])
        with pytest.raises(ValueError, match="ticker"):
            compute_weights(sig, RiskSettings())

    def test_missing_trade_date_column(self) -> None:
        sig = _signals([0.6]).drop(columns=["trade_date"])
        with pytest.raises(ValueError, match="trade_date"):
            compute_weights(sig, RiskSettings())

    def test_unknown_method_raises(self) -> None:
        sig = _signals([0.6])
        bad = RiskSettings(staking_method="kelly")
        object.__setattr__(bad, "staking_method", "not_a_method")
        with pytest.raises(ValueError, match="Unknown staking method"):
            compute_weights(sig, bad)

    def test_empty_input_returns_empty(self) -> None:
        sig = pd.DataFrame(
            {
                "ticker": pd.Series([], dtype=str),
                "trade_date": pd.Series([], dtype="object"),
                "prob": pd.Series([], dtype=float),
            }
        )
        w = compute_weights(sig, RiskSettings())
        assert len(w) == 0
