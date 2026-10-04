"""Unit tests for tracking.registry. ADR 0015 §7."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.config import BacktestSettings
from tracking.config import TrackingSettings
from tracking.registry import (
    _feature_cols,
    log_and_register_model,
    refit_full_model,
    register_model,
)

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


def _features(n: int = 3000, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    f1 = rng.normal(0, 1, n)
    f2 = rng.normal(0, 1, n)
    logit = 1.5 * f1 + 0.1 * f2
    p = 1.0 / (1.0 + np.exp(-logit))
    y = (rng.uniform(0, 1, n) < p).astype(int)
    return pd.DataFrame(
        {
            "ticker": "X",
            "trade_date": rng.choice(
                pd.date_range("2023-01-02", periods=252, freq="B").date,
                size=n,
            ),
            "feat1": f1,
            "feat2": f2,
            "next_return": rng.normal(0, 0.01, n),
            "next_return_positive": y,
        }
    )


@pytest.fixture
def bt() -> BacktestSettings:
    return BacktestSettings(use_time_decay=False)


# ---------------------------------------------------------------------
# _feature_cols
# ---------------------------------------------------------------------


class TestFeatureCols:
    def test_excludes_keys_and_labels(self) -> None:
        df = pd.DataFrame(
            {
                "ticker": ["A"],
                "trade_date": [pd.to_datetime("2024-01-02").date()],
                "next_return": [0.01],
                "next_return_positive": [1],
                "a": [1.0],
                "b": [2.0],
            }
        )
        cols = _feature_cols(df, "next_return_positive")
        assert cols == ["a", "b"]


# ---------------------------------------------------------------------
# refit_full_model
# ---------------------------------------------------------------------


class TestRefitFullModel:
    def test_returns_pipeline_and_cols(self, bt: BacktestSettings) -> None:
        pipe, cols = refit_full_model(_features(), bt)
        assert [s[0] for s in pipe.steps] == ["imputer", "scaler", "clf"]
        assert cols == ["feat1", "feat2"]

    def test_learns_signal(self, bt: BacktestSettings) -> None:
        df = _features()
        pipe, cols = refit_full_model(df, bt)
        probs = pipe.predict_proba(df[cols].to_numpy())[:, 1]
        corr = np.corrcoef(probs, df["feat1"].to_numpy())[0, 1]
        assert corr > 0.5

    def test_deterministic(self, bt: BacktestSettings) -> None:
        df = _features()
        p1, c1 = refit_full_model(df, bt)
        p2, c2 = refit_full_model(df, bt)
        assert c1 == c2
        probs1 = p1.predict_proba(df[c1].to_numpy())[:, 1]
        probs2 = p2.predict_proba(df[c2].to_numpy())[:, 1]
        np.testing.assert_allclose(probs1, probs2, atol=1e-12)

    def test_empty_features_raises(self, bt: BacktestSettings) -> None:
        with pytest.raises(ValueError, match="empty"):
            refit_full_model(pd.DataFrame(), bt)

    def test_single_class_raises(self, bt: BacktestSettings) -> None:
        df = _features()
        df["next_return_positive"] = 1
        with pytest.raises(ValueError, match="class"):
            refit_full_model(df, bt)

    def test_purity(self, bt: BacktestSettings) -> None:
        df = _features()
        before = df.copy()
        _ = refit_full_model(df, bt)
        pd.testing.assert_frame_equal(df, before)


# ---------------------------------------------------------------------
# register_model
# ---------------------------------------------------------------------


class TestRegisterModelDisabledPaths:
    def test_disabled_returns_none(self, bt: BacktestSettings) -> None:
        df = _features()
        pipe, cols = refit_full_model(df, bt)
        v = register_model(
            pipe,
            cols,
            df,
            TrackingSettings(enabled=False),
        )
        assert v is None

    def test_register_model_false_returns_none(self, bt: BacktestSettings) -> None:
        df = _features()
        pipe, cols = refit_full_model(df, bt)
        v = register_model(
            pipe,
            cols,
            df,
            TrackingSettings(enabled=True, register_model=False),
        )
        assert v is None

    def test_mlflow_missing_returns_none(
        self,
        bt: BacktestSettings,
        monkeypatch,
    ) -> None:
        from tracking import registry

        monkeypatch.setattr(registry, "MLFLOW_AVAILABLE", False)
        df = _features()
        pipe, cols = refit_full_model(df, bt)
        v = register_model(
            pipe,
            cols,
            df,
            TrackingSettings(enabled=True, register_model=True),
        )
        assert v is None


# ---------------------------------------------------------------------
# log_and_register_model
# ---------------------------------------------------------------------


class TestLogAndRegisterModelDisabled:
    def test_disabled_returns_none(self, bt: BacktestSettings) -> None:
        class R:
            run_id = "fake"

        v = log_and_register_model(
            R(),
            _features(),
            bt,
            TrackingSettings(enabled=False),
        )
        assert v is None

    def test_register_model_false_returns_none(self, bt: BacktestSettings) -> None:
        class R:
            run_id = "fake"

        v = log_and_register_model(
            R(),
            _features(),
            bt,
            TrackingSettings(enabled=True, register_model=False),
        )
        assert v is None

    def test_mlflow_missing_returns_none(
        self,
        bt: BacktestSettings,
        monkeypatch,
    ) -> None:
        from tracking import registry

        monkeypatch.setattr(registry, "MLFLOW_AVAILABLE", False)

        class R2:
            run_id = "fake"

        v = log_and_register_model(
            R2(),
            _features(),
            bt,
            TrackingSettings(enabled=True, register_model=True),
        )
        assert v is None
