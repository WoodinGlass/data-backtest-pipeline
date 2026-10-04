"""Unit tests for backtest.portfolio. ADR 0014 §7."""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.portfolio import (
    apply_rebalance_band,
    compute_portfolio_returns,
    run_portfolio,
    run_portfolio_from_weights,
)
from risk.config import RiskSettings

D = pd.to_datetime


# ---------------------------------------------------------------------
# apply_rebalance_band
# ---------------------------------------------------------------------


class TestRebalanceBand:
    def test_first_row_commits_target(self) -> None:
        idx = D(["2024-01-02", "2024-01-03", "2024-01-04"])
        target = pd.DataFrame({"T1": [0.05, 0.05, 0.05]}, index=idx)
        r = pd.DataFrame({"T1": [0.02, 0.50, 0.00]}, index=idx)
        committed, turnover = apply_rebalance_band(target, r, threshold=0.01)
        assert abs(committed.iloc[0, 0] - 0.05) < 1e-12
        assert abs(turnover.iloc[0] - 0.05) < 1e-12

    def test_drift_within_band_keeps_position(self) -> None:
        idx = D(["2024-01-02", "2024-01-03"])
        target = pd.DataFrame({"T1": [0.05, 0.05]}, index=idx)
        r = pd.DataFrame({"T1": [0.02, 0.0]}, index=idx)
        committed, turnover = apply_rebalance_band(target, r, threshold=0.01)
        drifted_1 = 0.05 * 1.02 / (1 + 0.05 * 0.02)
        assert abs(committed.iloc[1, 0] - drifted_1) < 1e-9
        assert abs(turnover.iloc[1]) < 1e-12

    def test_large_drift_commits_target(self) -> None:
        idx = D(["2024-01-02", "2024-01-03", "2024-01-04"])
        target = pd.DataFrame({"T1": [0.05, 0.05, 0.05]}, index=idx)
        r = pd.DataFrame({"T1": [0.02, 0.50, 0.0]}, index=idx)
        committed, _ = apply_rebalance_band(target, r, threshold=0.01)
        assert abs(committed.iloc[2, 0] - 0.05) < 1e-9

    def test_deterministic(self) -> None:
        idx = D(["2024-01-02", "2024-01-03"])
        target = pd.DataFrame({"T1": [0.05, 0.05]}, index=idx)
        r = pd.DataFrame({"T1": [0.01, 0.01]}, index=idx)
        a, _ = apply_rebalance_band(target, r, 0.01)
        b, _ = apply_rebalance_band(target, r, 0.01)
        pd.testing.assert_frame_equal(a, b)


# ---------------------------------------------------------------------
# compute_portfolio_returns
# ---------------------------------------------------------------------


class TestComputeReturns:
    def test_gross_equals_weight_times_return(self) -> None:
        idx = D(["2024-01-02"])
        w = pd.DataFrame({"T1": [0.05]}, index=idx)
        r = pd.DataFrame({"T1": [0.01]}, index=idx)
        turn = pd.Series([0.0], index=idx)
        out = compute_portfolio_returns(
            w,
            r,
            cash_annual_rates=None,
            one_way_cost_bp=2.5,
            turnover=turn,
        )
        assert abs(out.loc[idx[0], "gross"] - 0.0005) < 1e-12

    def test_cost_charged_on_turnover(self) -> None:
        idx = D(["2024-01-02"])
        w = pd.DataFrame({"T1": [0.05]}, index=idx)
        r = pd.DataFrame({"T1": [0.0]}, index=idx)
        turn = pd.Series([0.05], index=idx)
        out = compute_portfolio_returns(
            w,
            r,
            None,
            one_way_cost_bp=2.5,
            turnover=turn,
        )
        assert abs(out.loc[idx[0], "cost"] - 0.05 * 2.5 / 10000) < 1e-12

    def test_net_equals_gross_plus_cash_minus_cost(self) -> None:
        idx = D(["2024-01-02", "2024-01-03"])
        w = pd.DataFrame({"T1": [0.05, 0.05]}, index=idx)
        r = pd.DataFrame({"T1": [0.01, 0.005]}, index=idx)
        turn = pd.Series([0.05, 0.0], index=idx)
        out = compute_portfolio_returns(w, r, None, 2.5, turn)
        for d in idx:
            assert (
                abs(
                    out.loc[d, "net"]
                    - (out.loc[d, "gross"] + out.loc[d, "cash_ret"] - out.loc[d, "cost"])
                )
                < 1e-15
            )

    def test_cash_rate_applied_to_residual(self) -> None:
        idx = D(["2024-01-02"])
        w = pd.DataFrame({"T1": [0.05]}, index=idx)
        r = pd.DataFrame({"T1": [0.0]}, index=idx)
        turn = pd.Series([0.0], index=idx)
        cash = pd.Series(0.05, index=idx)  # 5% annual
        out = compute_portfolio_returns(w, r, cash, 2.5, turn)
        daily_rf = 1.05 ** (1.0 / 252) - 1.0
        expected = 0.95 * daily_rf
        assert abs(out.loc[idx[0], "cash_ret"] - expected) < 1e-12

    def test_empty_returns_empty(self) -> None:
        out = compute_portfolio_returns(
            pd.DataFrame(),
            pd.DataFrame(),
            None,
            2.5,
            pd.Series(dtype=float),
        )
        assert out.empty


# ---------------------------------------------------------------------
# run_portfolio_from_weights
# ---------------------------------------------------------------------


class TestRunFromWeights:
    def test_spy_buy_hold(self) -> None:
        idx = D(["2024-01-02", "2024-01-03", "2024-01-04"])
        tgt = pd.DataFrame({"SPY": [1.0, 1.0, 1.0]}, index=idx)
        r = pd.DataFrame({"SPY": [0.005, 0.003, -0.002]}, index=idx)
        res = run_portfolio_from_weights(tgt, r)
        assert res.n_days == 3
        assert (res.n_positions == 1).all()
        assert (res.cash_weight.abs() < 1e-12).all()

    def test_empty_inputs(self) -> None:
        res = run_portfolio_from_weights(pd.DataFrame(), pd.DataFrame())
        assert res.n_days == 0


# ---------------------------------------------------------------------
# run_portfolio (full pipeline)
# ---------------------------------------------------------------------


@pytest.fixture
def simple_inputs():
    dates = pd.date_range("2024-01-02", periods=3, freq="B").date
    preds = pd.DataFrame(
        {
            "ticker": ["T1"] * 3 + ["T2"] * 3 + ["SPY"] * 3,
            "trade_date": list(dates) * 3,
            "prob": [0.70, 0.70, 0.70, 0.30, 0.30, 0.30, 0.50, 0.50, 0.50],
            "is_benchmark": [False] * 6 + [True] * 3,
        }
    )
    prices = pd.DataFrame(
        {
            "ticker": ["T1"] * 3 + ["T2"] * 3 + ["SPY"] * 3,
            "trade_date": list(dates) * 3,
            "close": [100.0] * 9,
        }
    )
    nr = pd.DataFrame(
        {
            "ticker": ["T1"] * 3 + ["T2"] * 3 + ["SPY"] * 3,
            "trade_date": list(dates) * 3,
            "next_return": [0.01] * 3 + [0.0] * 3 + [0.005] * 3,
        }
    )
    return preds, prices, nr


class TestRunPortfolio:
    def test_basic(self, simple_inputs) -> None:
        preds, prices, nr = simple_inputs
        res = run_portfolio(preds, prices, nr)
        assert res.n_days == 3
        assert "T1" in res.weights.columns

    def test_benchmark_never_selected(self, simple_inputs) -> None:
        preds, prices, nr = simple_inputs
        preds = preds.copy()
        preds.loc[preds["ticker"] == "SPY", "prob"] = 0.99
        res = run_portfolio(preds, prices, nr)
        assert "SPY" not in res.weights.columns

    def test_kelly_cap_applied(self, simple_inputs) -> None:
        preds, prices, nr = simple_inputs
        res = run_portfolio(preds, prices, nr)
        assert abs(res.weights["T1"].iloc[0] - 0.05) < 1e-9

    def test_custom_kelly_cap(self, simple_inputs) -> None:
        preds, prices, nr = simple_inputs
        rs = RiskSettings(kelly_cap=0.10)
        res = run_portfolio(preds, prices, nr, risk_settings=rs)
        assert abs(res.weights["T1"].iloc[0] - 0.10) < 1e-9

    def test_threshold_above_all_probs(self, simple_inputs) -> None:
        preds, prices, nr = simple_inputs
        rs = RiskSettings(entry_prob_threshold=0.99)
        res = run_portfolio(preds, prices, nr, risk_settings=rs)
        assert res.n_days == 0

    def test_purity(self, simple_inputs) -> None:
        preds, prices, nr = simple_inputs
        pb, prb, nb = preds.copy(), prices.copy(), nr.copy()
        _ = run_portfolio(preds, prices, nr)
        pd.testing.assert_frame_equal(preds, pb)
        pd.testing.assert_frame_equal(prices, prb)
        pd.testing.assert_frame_equal(nr, nb)

    def test_empty_predictions(self) -> None:
        empty = pd.DataFrame(
            {
                "ticker": pd.Series([], dtype=str),
                "trade_date": pd.Series([], dtype=object),
                "prob": pd.Series([], dtype=float),
            }
        )
        res = run_portfolio(empty, pd.DataFrame(), pd.DataFrame())
        assert res.n_days == 0
