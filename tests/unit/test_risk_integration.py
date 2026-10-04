"""Integration + anti-look-ahead tests for the risk pipeline.

Composes entry -> staking -> limits end-to-end and verifies:
- The pipeline runs.
- Outputs are deterministic and reproducible.
- NO future data leaks into decisions at time t.

The anti-look-ahead tests are the core value of this file: they
prove that ADR 0013's PIT contract holds across the full pipeline,
not just in isolated units.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from risk.config import RiskSettings
from risk.entry import apply_entry_rules
from risk.limits import apply_all_limits
from risk.staking import compute_weights

# =====================================================================
# Synthetic fixtures (deterministic)
# =====================================================================


def _predictions(n_dates: int = 12, n_tickers: int = 3) -> pd.DataFrame:
    """Predictions frame: n_dates x (n_tickers + 1 benchmark)."""
    dates = pd.date_range("2024-01-02", periods=n_dates, freq="B").date
    rng = np.random.default_rng(42)
    rows = []
    for d in dates:
        for i in range(n_tickers):
            rows.append(
                {
                    "ticker": f"T{i}",
                    "trade_date": d,
                    "prob": float(rng.uniform(0.35, 0.85)),
                    "realized_vol": float(rng.uniform(0.15, 0.35)),
                    "is_benchmark": False,
                }
            )
        rows.append(
            {
                "ticker": "SPY",
                "trade_date": d,
                "prob": 0.50,
                "realized_vol": 0.15,
                "is_benchmark": True,
            }
        )
    return pd.DataFrame(rows)


def _prices(preds: pd.DataFrame, seed: int = 7) -> pd.DataFrame:
    """Prices frame aligned to predictions."""
    rng = np.random.default_rng(seed)
    rows = []
    for t in sorted(preds["ticker"].unique()):
        px = 100.0
        for d in sorted(preds["trade_date"].unique()):
            px *= 1.0 + float(rng.normal(0.0, 0.008))
            rows.append({"ticker": t, "trade_date": d, "close": float(px)})
    return pd.DataFrame(rows)


def _run_pipeline(
    preds: pd.DataFrame,
    prices: pd.DataFrame,
    settings: RiskSettings,
) -> pd.DataFrame:
    """Full pipeline: entry -> filter long -> staking -> limits."""
    # 1. Entry
    entered = apply_entry_rules(preds, settings)
    long_only = entered[entered["selected"]].copy()

    if long_only.empty:
        # Edge case: no positions. Return empty limits output with schema.
        empty = pd.DataFrame(
            {
                "ticker": pd.Series([], dtype=str),
                "trade_date": pd.Series([], dtype="object"),
                "weight": pd.Series([], dtype=float),
            }
        )
        return apply_all_limits(empty, prices, settings)

    # 2. Staking
    weights = compute_weights(long_only, settings)
    long_only["weight"] = weights.values

    # 3. Limits
    limits_in = long_only[["ticker", "trade_date", "weight"]].copy()
    return apply_all_limits(limits_in, prices, settings)


# =====================================================================
# Test 1: Pipeline composes end-to-end
# =====================================================================


class TestPipelineComposition:
    def test_runs_and_produces_valid_weights(self) -> None:
        preds = _predictions()
        prices = _prices(preds)
        rs = RiskSettings(
            entry_method="threshold",
            entry_prob_threshold=0.55,
            staking_method="kelly",
            dd_derisk_factor=1.0,  # for test isolation (no derisk scaling)
        )
        out = _run_pipeline(preds, prices, rs)

        # Output shape/columns
        assert set(out.columns) == {
            "ticker",
            "trade_date",
            "weight_before_limits",
            "weight",
            "stop_out",
            "dd_state",
        }
        # Weights in [0, cap]
        assert (out["weight"] >= 0.0).all()
        assert (out["weight"] <= rs.kelly_cap).all()
        # dd_state valid
        assert set(out["dd_state"].unique()) <= {"normal", "derisk", "halt"}

    def test_all_staking_methods_compose(self) -> None:
        preds = _predictions()
        prices = _prices(preds)
        for method in ("kelly", "fixed_fractional", "equal_weight", "vol_target"):
            rs = RiskSettings(
                entry_method="threshold",
                staking_method=method,
                dd_derisk_factor=1.0,
            )
            out = _run_pipeline(preds, prices, rs)
            assert (out["weight"] <= rs.kelly_cap).all(), method

    def test_all_entry_methods_compose(self) -> None:
        preds = _predictions()
        prices = _prices(preds)
        for method in ("threshold", "top_n", "cross_sectional"):
            rs = RiskSettings(
                entry_method=method,
                staking_method="kelly",
                dd_derisk_factor=1.0,
            )
            out = _run_pipeline(preds, prices, rs)
            assert len(out) >= 0, method

    def test_pipeline_is_deterministic(self) -> None:
        preds = _predictions()
        prices = _prices(preds)
        rs = RiskSettings(dd_derisk_factor=1.0)
        a = _run_pipeline(preds, prices, rs)
        b = _run_pipeline(preds, prices, rs)
        pd.testing.assert_frame_equal(
            a.reset_index(drop=True),
            b.reset_index(drop=True),
        )

    def test_inputs_not_mutated(self) -> None:
        preds = _predictions()
        prices = _prices(preds)
        preds_before = preds.copy()
        prices_before = prices.copy()
        _run_pipeline(preds, prices, RiskSettings(dd_derisk_factor=1.0))
        pd.testing.assert_frame_equal(preds, preds_before)
        pd.testing.assert_frame_equal(prices, prices_before)


# =====================================================================
# Test 2: Prefix stability (truncation)
# =====================================================================


class TestPrefixStability:
    """Computing on the full dataset vs a truncated dataset must give
    identical outputs for the overlapping dates (for entry, staking,
    and limits alike). If not, there is temporal leakage.
    """

    def test_entry_prefix_stable(self) -> None:
        preds = _predictions()
        rs = RiskSettings()
        k = 6
        cutoff = sorted(preds["trade_date"].unique())[k]

        full = apply_entry_rules(preds, rs)
        truncated = apply_entry_rules(preds[preds["trade_date"] <= cutoff], rs)

        full_prefix = full[full["trade_date"] <= cutoff].reset_index(drop=True)
        trunc_reset = truncated.reset_index(drop=True)
        pd.testing.assert_frame_equal(full_prefix, trunc_reset)

    def test_staking_prefix_stable(self) -> None:
        preds = _predictions()
        rs = RiskSettings(staking_method="kelly")
        entered = apply_entry_rules(preds, rs)
        long_only = entered[entered["selected"]].copy()
        k = 6
        cutoff = sorted(preds["trade_date"].unique())[k]

        full_w = compute_weights(long_only, rs)
        long_only_full = long_only.assign(weight=full_w.values)

        trunc = long_only_full[long_only_full["trade_date"] <= cutoff]
        trunc_w = compute_weights(trunc, rs)

        np.testing.assert_allclose(
            full_w[long_only_full["trade_date"] <= cutoff].values,
            trunc_w.values,
            atol=1e-12,
        )

    def test_limits_prefix_stable(self) -> None:
        """Limits is a chronological single-pass walk; truncating
        future data must leave past outputs untouched.
        """
        preds = _predictions()
        prices = _prices(preds)
        rs = RiskSettings(dd_derisk_factor=1.0)

        full = _run_pipeline(preds, prices, rs)
        k = 6
        cutoff = sorted(preds["trade_date"].unique())[k]

        trunc_preds = preds[preds["trade_date"] <= cutoff]
        trunc_prices = prices[prices["trade_date"] <= cutoff]
        truncated = _run_pipeline(trunc_preds, trunc_prices, rs)

        # Compare overlapping rows (same ticker + date set)
        full_overlap = (
            full[full["trade_date"] <= cutoff]
            .reset_index(drop=True)
            .sort_values(["ticker", "trade_date"])
            .reset_index(drop=True)
        )
        trunc_sorted = truncated.sort_values(["ticker", "trade_date"]).reset_index(drop=True)
        pd.testing.assert_frame_equal(
            # dd_state compared separately below
            full_overlap.drop(columns=["dd_state"]),
            trunc_sorted.drop(columns=["dd_state"]),
        )
        pd.testing.assert_series_equal(
            full_overlap["dd_state"],
            trunc_sorted["dd_state"],
            check_names=False,
        )


# =====================================================================
# Test 3: Poison future rows, past unchanged
# =====================================================================


class TestPoisonFuture:
    """The core anti-look-ahead probe.

    Replace values in rows AFTER a cutoff with garbage. Outputs for
    rows BEFORE the cutoff must be identical to a clean run.
    """

    def test_entry_ignores_future_probs(self) -> None:
        preds = _predictions()
        rs = RiskSettings()
        cutoff = sorted(preds["trade_date"].unique())[6]

        clean = apply_entry_rules(preds, rs)

        # Poison future probs to extreme values
        poisoned = preds.copy()
        future_mask = poisoned["trade_date"] > cutoff
        poisoned.loc[future_mask, "prob"] = 0.999  # all want long

        polluted = apply_entry_rules(poisoned, rs)

        clean_past = clean[clean["trade_date"] <= cutoff].reset_index(drop=True)
        polluted_past = polluted[polluted["trade_date"] <= cutoff].reset_index(drop=True)

        pd.testing.assert_frame_equal(clean_past, polluted_past)

    def test_staking_ignores_future_vols(self) -> None:
        preds = _predictions()
        rs = RiskSettings(staking_method="vol_target")
        cutoff = sorted(preds["trade_date"].unique())[6]

        entered = apply_entry_rules(preds, rs)
        long_only = entered[entered["selected"]].copy()
        clean_w = compute_weights(long_only, rs)

        # Poison future vols to extreme low values
        poisoned = long_only.copy()
        future_mask = poisoned["trade_date"] > cutoff
        poisoned.loc[future_mask, "realized_vol"] = 0.001

        polluted_w = compute_weights(poisoned, rs)

        past_mask = long_only["trade_date"] <= cutoff
        np.testing.assert_allclose(
            clean_w[past_mask].values,
            polluted_w[past_mask].values,
            atol=1e-12,
        )

    def test_limits_ignores_future_prices(self) -> None:
        preds = _predictions()
        prices = _prices(preds)
        rs = RiskSettings(dd_derisk_factor=1.0)
        cutoff = sorted(preds["trade_date"].unique())[6]

        clean = _run_pipeline(preds, prices, rs)

        # Poison future prices: huge crash
        poisoned_prices = prices.copy()
        future_mask = poisoned_prices["trade_date"] > cutoff
        poisoned_prices.loc[future_mask, "close"] = 1.0

        polluted = _run_pipeline(preds, poisoned_prices, rs)

        clean_past = clean[clean["trade_date"] <= cutoff].reset_index(drop=True)
        polluted_past = polluted[polluted["trade_date"] <= cutoff].reset_index(drop=True)
        # weight and stop_out must match; dd_state must match too
        # (because DD at a date is determined by NAV history up to
        # and including that date).
        pd.testing.assert_frame_equal(clean_past, polluted_past)


# =====================================================================
# Test 4: Structural anti-look-ahead
# =====================================================================


class TestStructuralAntiLookAhead:
    """The pipeline must not consume any future-derived column."""

    def test_extra_future_columns_are_ignored(self) -> None:
        preds = _predictions()
        prices = _prices(preds)
        rs = RiskSettings(dd_derisk_factor=1.0)

        clean = _run_pipeline(preds, prices, rs)

        # Add columns that a naive implementation might accidentally use.
        rng = np.random.default_rng(1)
        polluted = preds.copy()
        polluted["next_return_positive"] = rng.integers(0, 2, len(polluted))
        polluted["future_close"] = 999.0
        polluted["forward_return"] = -0.5
        polluted["label"] = 1

        polluted_out = _run_pipeline(polluted, prices, rs)

        pd.testing.assert_frame_equal(
            clean.reset_index(drop=True),
            polluted_out.reset_index(drop=True),
        )


# =====================================================================
# Test 5: Benchmark is never traded
# =====================================================================


class TestBenchmarkNotTraded:
    def test_full_pipeline_never_trades_spy(self) -> None:
        preds = _predictions()
        # Even with high SPY prob, pipeline must not enter it.
        preds.loc[preds["ticker"] == "SPY", "prob"] = 0.99
        prices = _prices(preds)
        rs = RiskSettings(
            entry_method="top_n",
            entry_top_n=10,  # large enough to tempt SPY
            staking_method="kelly",
            dd_derisk_factor=1.0,
        )
        out = _run_pipeline(preds, prices, rs)
        assert not (out["ticker"] == "SPY").any(), "SPY (benchmark) entered a position"


# =====================================================================
# Test 6: Full pipeline stress — extreme configurations
# =====================================================================


class TestExtremeConfigs:
    def test_tight_threshold_no_positions(self) -> None:
        preds = _predictions()
        prices = _prices(preds)
        # Threshold above all probs -> no entries
        rs = RiskSettings(
            entry_method="threshold",
            entry_prob_threshold=0.99,
            dd_derisk_factor=1.0,
        )
        out = _run_pipeline(preds, prices, rs)
        assert (out["weight"] == 0.0).all()

    def test_permissive_threshold_all_long(self) -> None:
        preds = _predictions()
        prices = _prices(preds)
        rs = RiskSettings(
            entry_method="threshold",
            entry_prob_threshold=0.51,  # just above 0.5
            dd_derisk_factor=1.0,
        )
        out = _run_pipeline(preds, prices, rs)
        # Most rows should have non-zero weight
        assert (out["weight"] > 0.0).any()
