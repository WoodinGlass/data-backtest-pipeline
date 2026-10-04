"""Unit tests for backtest.metrics. ADR 0014 §6."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest import metrics

# ---------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------


class TestClassification:
    def test_brier_hand_computed(self) -> None:
        y = np.array([0, 1, 0, 1, 1])
        p = np.array([0.4, 0.7, 0.6, 0.3, 0.9])
        assert abs(metrics.brier(y, p) - np.mean((p - y) ** 2)) < 1e-12

    def test_hit_rate_hand_computed(self) -> None:
        y = np.array([0, 1, 0, 1, 1])
        p = np.array([0.4, 0.7, 0.6, 0.3, 0.9])
        assert abs(metrics.hit_rate(y, p) - 0.6) < 1e-12

    def test_auc_perfect_separation(self) -> None:
        y = np.array([0, 0, 1, 1])
        p = np.array([0.1, 0.2, 0.8, 0.9])
        assert metrics.auc(y, p) == 1.0

    def test_single_class_returns_nan(self) -> None:
        assert np.isnan(metrics.auc(np.array([1, 1, 1]), np.array([0.5, 0.6, 0.7])))
        assert np.isnan(metrics.log_loss(np.array([1, 1]), np.array([0.5, 0.6])))

    def test_calibration_slope_intercept_on_calibrated(self) -> None:
        rng = np.random.default_rng(0)
        n = 5000
        p = rng.uniform(0.05, 0.95, n)
        y = (rng.uniform(0, 1, n) < p).astype(int)
        slope, intercept = metrics.calibration_slope_intercept(y, p)
        assert abs(slope - 1.0) < 0.15
        assert abs(intercept) < 0.15

    def test_classification_metrics_keys(self) -> None:
        y = np.array([0, 1, 0, 1, 1])
        p = np.array([0.4, 0.7, 0.6, 0.3, 0.9])
        cm = metrics.classification_metrics(y, p)
        for k in (
            "log_loss",
            "brier",
            "auc",
            "hit_rate",
            "calibration_slope",
            "calibration_intercept",
            "n_obs",
        ):
            assert k in cm


# ---------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------


class TestTrading:
    def test_sharpe_constant_returns_zero(self) -> None:
        # Near-constant float series: sd ~ 1e-19, tolerance catches it
        assert metrics.sharpe(np.array([0.001] * 100)) == 0.0

    def test_sharpe_zero_mean_is_zero(self) -> None:
        assert abs(metrics.sharpe(np.array([0.01, -0.01] * 50))) < 1e-12

    def test_sharpe_hand_computed(self) -> None:
        r = np.array([0.01, 0.02, -0.01, 0.00, 0.015])
        manual = r.mean() / r.std(ddof=1) * np.sqrt(252)
        assert abs(metrics.sharpe(r) - manual) < 1e-12

    def test_max_drawdown_hand_computed(self) -> None:
        assert abs(metrics.max_drawdown(np.array([0.10, -0.10, 0.05])) - 0.10) < 1e-9

    def test_cagr_hand_computed(self) -> None:
        c = metrics.cagr(np.array([0.001] * 504))
        assert abs(c - 0.286) < 0.01

    def test_turnover_per_date(self) -> None:
        w = pd.DataFrame(
            {"A": [0.05, 0.05, 0.00], "B": [0.05, 0.03, 0.00]},
            index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
        )
        t = metrics.turnover_per_date(w)
        assert abs(t.iloc[0] - 0.10) < 1e-12
        assert abs(t.iloc[1] - 0.02) < 1e-12
        assert abs(t.iloc[2] - 0.08) < 1e-12

    def test_cost_adjusted_return(self) -> None:
        gross = np.array([0.005, 0.003, -0.002])
        t = np.array([0.10, 0.02, 0.08])
        net = metrics.cost_adjusted_return(gross, t, one_way_cost_bp=2.5)
        np.testing.assert_allclose(net, gross - t * 2.5 / 10000.0)

    def test_trading_metrics_bundle(self) -> None:
        r = np.array([0.01, -0.005, 0.003, 0.0, 0.015])
        tm = metrics.trading_metrics(r)
        for k in ("sharpe", "cagr", "max_drawdown", "total_return", "n_days"):
            assert k in tm


# ---------------------------------------------------------------------
# Advanced
# ---------------------------------------------------------------------


class TestBootstrap:
    def test_ci_contains_point(self) -> None:
        rng = np.random.default_rng(0)
        r = rng.normal(0.0005, 0.01, 500)
        pt, lo, hi = metrics.bootstrap_sharpe_ci(r, n_resamples=200, seed=1, block=5)
        assert lo <= pt <= hi

    def test_zero_mean_ci_includes_zero(self) -> None:
        rng = np.random.default_rng(1)
        r = rng.normal(0.0, 0.01, 500)
        _pt, lo, hi = metrics.bootstrap_sharpe_ci(r, n_resamples=200, seed=2, block=5)
        assert lo < 0 < hi

    def test_too_few_obs_returns_nan(self) -> None:
        pt, lo, hi = metrics.bootstrap_sharpe_ci(np.array([0.01, 0.02]))
        assert np.isnan(pt) and np.isnan(lo) and np.isnan(hi)


class TestDeflatedSharpe:
    def test_more_trials_lowers_dsr(self) -> None:
        high = metrics.deflated_sharpe(0.15, n_trials=4, n_obs=1000)
        low = metrics.deflated_sharpe(0.15, n_trials=1000, n_obs=1000)
        assert high > low

    def test_negative_sharpe_low_dsr(self) -> None:
        assert metrics.deflated_sharpe(-0.05, n_trials=4, n_obs=1000) < 0.1

    def test_zero_trials_returns_nan(self) -> None:
        assert np.isnan(metrics.deflated_sharpe(0.1, n_trials=0, n_obs=1000))

    def test_realized_skew_kurt(self) -> None:
        rng = np.random.default_rng(0)
        r = rng.normal(0, 1, 10000)
        skew, kurt = metrics.realized_skew_kurt(r)
        assert abs(skew) < 0.1
        assert abs(kurt - 3.0) < 0.2


# ---------------------------------------------------------------------
# Purity + edge cases
# ---------------------------------------------------------------------


class TestPurityAndEdges:
    def test_classification_does_not_mutate(self) -> None:
        y = np.array([0, 1, 0, 1, 1])
        p = np.array([0.4, 0.7, 0.6, 0.3, 0.9])
        y_bak, p_bak = y.copy(), p.copy()
        _ = metrics.classification_metrics(y, p)
        np.testing.assert_array_equal(y, y_bak)
        np.testing.assert_array_equal(p, p_bak)

    def test_empty_arrays_return_nan(self) -> None:
        assert np.isnan(metrics.sharpe(np.array([])))
        assert np.isnan(metrics.cagr(np.array([])))
        assert np.isnan(metrics.max_drawdown(np.array([])))
        assert np.isnan(metrics.log_loss(np.array([]), np.array([])))

    def test_deterministic(self) -> None:
        r = np.array([0.01, -0.005, 0.003, 0.0, 0.015])
        assert metrics.sharpe(r) == metrics.sharpe(r)
