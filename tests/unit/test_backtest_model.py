"""Unit tests for backtest.model. ADR 0014 §3, §4."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.config import BacktestSettings
from backtest.model import (
    build_estimator,
    compute_time_decay_weights,
    fit_predict,
)


class TestTimeDecayWeights:
    def test_half_life_5_with_11_dates(self) -> None:
        # Oldest row is 10 trade days back; half_life=5 -> 2^-2 = 0.25
        dates = pd.Series(pd.date_range("2024-01-02", periods=11, freq="B").date)
        w = compute_time_decay_weights(dates, half_life_days=5)
        assert abs(w[0] - 0.25) < 1e-12
        assert abs(w[-1] - 1.0) < 1e-12

    def test_half_life_10_with_11_dates(self) -> None:
        dates = pd.Series(pd.date_range("2024-01-02", periods=11, freq="B").date)
        w = compute_time_decay_weights(dates, half_life_days=10)
        # Oldest is 10 trade days back = exactly 1 half-life -> 0.5
        assert abs(w[0] - 0.5) < 1e-12

    def test_same_date_gives_uniform_weight(self) -> None:
        dates = pd.Series([pd.date_range("2024-01-02", periods=1).date[0]] * 5)
        w = compute_time_decay_weights(dates, half_life_days=5)
        assert (w == 1.0).all()

    def test_empty_returns_empty(self) -> None:
        assert compute_time_decay_weights(pd.Series([], dtype=object), 5).size == 0

    def test_zero_half_life_raises(self) -> None:
        with pytest.raises(ValueError):
            compute_time_decay_weights(
                pd.Series(pd.date_range("2024-01-02", periods=3).date),
                half_life_days=0,
            )


class TestBuildEstimator:
    def test_steps(self) -> None:
        est = build_estimator(BacktestSettings())
        assert [name for name, _ in est.steps] == ["imputer", "scaler", "clf"]

    def test_non_logistic_raises(self) -> None:
        bs = BacktestSettings()
        object.__setattr__(bs, "model_type", "gbm")
        with pytest.raises(ValueError, match="not supported"):
            build_estimator(bs)


@pytest.fixture
def synthetic_data():
    rng = np.random.default_rng(0)
    n_train, n_test = 2000, 500

    x1_tr = rng.normal(0, 1, n_train)
    x2_tr = rng.normal(0, 1, n_train)
    logit = 2.0 * x1_tr + 0.1 * x2_tr
    p = 1.0 / (1.0 + np.exp(-logit))
    y = (rng.uniform(0, 1, n_train) < p).astype(int)

    train = pd.DataFrame(
        {
            "ticker": "X",
            "trade_date": rng.choice(
                pd.date_range("2023-01-02", periods=252, freq="B").date,
                size=n_train,
            ),
            "feat1": x1_tr,
            "feat2": x2_tr,
            "next_return_positive": y,
        }
    )

    x1_te = rng.normal(0, 1, n_test)
    x2_te = rng.normal(0, 1, n_test)
    test = pd.DataFrame(
        {
            "ticker": "X",
            "trade_date": pd.date_range("2024-01-02", periods=n_test, freq="B").date,
            "feat1": x1_te,
            "feat2": x2_te,
        }
    )

    return train, test, x1_te


class TestFitPredict:
    def test_shape_and_range(self, synthetic_data) -> None:
        train, test, _ = synthetic_data
        p = fit_predict(train, test, ["feat1", "feat2"])
        assert p.shape == (len(test),)
        assert (p >= 0).all() and (p <= 1).all()

    def test_learns_signal(self, synthetic_data) -> None:
        train, test, x1_te = synthetic_data
        p = fit_predict(train, test, ["feat1", "feat2"])
        corr = np.corrcoef(p, x1_te)[0, 1]
        assert corr > 0.5, f"corr too low: {corr}"

    def test_deterministic(self, synthetic_data) -> None:
        train, test, _ = synthetic_data
        p1 = fit_predict(train, test, ["feat1", "feat2"])
        p2 = fit_predict(train, test, ["feat1", "feat2"])
        np.testing.assert_allclose(p1, p2, atol=1e-12)

    def test_nan_handled(self, synthetic_data) -> None:
        train, test, _ = synthetic_data
        train = train.copy()
        train.loc[:50, "feat1"] = np.nan
        test = test.copy()
        test.loc[:10, "feat1"] = np.nan
        p = fit_predict(train, test, ["feat1", "feat2"])
        assert np.isfinite(p).all()

    def test_all_nan_column(self, synthetic_data) -> None:
        train, test, _ = synthetic_data
        train = train.copy()
        train["feat2"] = np.nan
        test = test.copy()
        test["feat2"] = np.nan
        p = fit_predict(train, test, ["feat1", "feat2"])
        assert np.isfinite(p).all()

    def test_single_class_returns_base_rate(self, synthetic_data) -> None:
        train, test, _ = synthetic_data
        train = train.copy()
        train["next_return_positive"] = 1
        p = fit_predict(train, test, ["feat1", "feat2"])
        assert (p == 1.0).all()

    def test_no_time_decay(self, synthetic_data) -> None:
        train, test, _ = synthetic_data
        bs = BacktestSettings(use_time_decay=False)
        p = fit_predict(train, test, ["feat1", "feat2"], bs)
        assert p.shape == (len(test),)

    def test_purity(self, synthetic_data) -> None:
        train, test, _ = synthetic_data
        tr_bak, te_bak = train.copy(), test.copy()
        _ = fit_predict(train, test, ["feat1", "feat2"])
        pd.testing.assert_frame_equal(train, tr_bak)
        pd.testing.assert_frame_equal(test, te_bak)

    def test_missing_label_raises(self, synthetic_data) -> None:
        train, test, _ = synthetic_data
        with pytest.raises(ValueError):
            fit_predict(
                train.drop(columns=["next_return_positive"]),
                test,
                ["feat1", "feat2"],
            )

    def test_missing_feature_raises(self, synthetic_data) -> None:
        train, test, _ = synthetic_data
        with pytest.raises(ValueError):
            fit_predict(train, test, ["nonexistent"])

    def test_empty_test_returns_empty(self, synthetic_data) -> None:
        train, test, _ = synthetic_data
        p = fit_predict(train, test.iloc[0:0], ["feat1", "feat2"])
        assert p.size == 0
