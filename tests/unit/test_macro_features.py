"""Unit tests for features/macro_features.py.

Includes anti-leakage: poisoning macro_df rows after time T must not
change feature values at T.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from features.config import FeatureSettings
from features.macro_features import (
    add_macro_features,
    macro_feature_columns,
)


def _prices_df(
    *,
    ticker: str = "AAPL",
    n: int = 500,
    seed: int = 0,
) -> pd.DataFrame:
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    return pd.DataFrame({"ticker": [ticker] * n, "trade_date": dates})


def _macro_df(n: int = 500, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    # Constant-ish values; YoY must be near zero for constant series
    cpi = 250.0 * (1.0 + 0.02 * np.arange(n) / 252.0)
    pay = 150_000.0 * (1.0 + 0.01 * np.arange(n) / 252.0)
    return pd.DataFrame(
        {
            "trade_date": dates,
            "macro_fedfunds": np.full(n, 5.0),
            "macro_dgs10": 4.0 + rng.normal(0, 0.1, n),
            "macro_dgs2": 4.5 + rng.normal(0, 0.1, n),
            "macro_cpiaucsl": cpi,
            "macro_payems": pay,
            "macro_unrate": np.full(n, 4.0),
        }
    )


@pytest.fixture
def settings() -> FeatureSettings:
    return FeatureSettings()


# ─── contract ───────────────────────────────────────────────
def test_missing_df_columns_raises(settings: FeatureSettings) -> None:
    df = pd.DataFrame({"foo": [1]})
    with pytest.raises(ValueError, match="'ticker' and 'trade_date'"):
        add_macro_features(df, _macro_df(), settings=settings)


def test_missing_macro_columns_raises(settings: FeatureSettings) -> None:
    macro = _macro_df().drop(columns=["macro_dgs10"])
    with pytest.raises(ValueError, match="missing columns"):
        add_macro_features(_prices_df(), macro, settings=settings)


def test_returns_all_declared_features(settings: FeatureSettings) -> None:
    out = add_macro_features(_prices_df(), _macro_df(), settings=settings)
    for col in macro_feature_columns(settings):
        assert col in out.columns


def test_left_join_keeps_all_price_rows(settings: FeatureSettings) -> None:
    df = _prices_df(n=100)
    out = add_macro_features(df, _macro_df(n=100), settings=settings)
    assert len(out) == len(df)


# ─── per-feature ────────────────────────────────────────────
def test_passthrough_matches_source(settings: FeatureSettings) -> None:
    df = _prices_df(n=100)
    macro = _macro_df(n=100)
    out = add_macro_features(df, macro, settings=settings)
    # First row should have macro_fedfunds == 5.0
    assert out.iloc[0]["mc_fedfunds"] == pytest.approx(5.0)
    assert out.iloc[0]["mc_unrate"] == pytest.approx(4.0)


def test_yoy_boundary_nan(settings: FeatureSettings) -> None:
    out = add_macro_features(_prices_df(n=500), _macro_df(n=500), settings=settings)
    col = "mc_cpi_yoy"
    # First 252 rows must be NaN
    assert out[col].iloc[:252].isna().all()
    assert out[col].iloc[252:].notna().all()


def test_yoy_matches_manual_calc(settings: FeatureSettings) -> None:
    df = _prices_df(n=500)
    macro = _macro_df(n=500)
    out = add_macro_features(df, macro, settings=settings)
    manual = (macro["macro_cpiaucsl"] / macro["macro_cpiaucsl"].shift(252) - 1.0) * 100.0
    np.testing.assert_allclose(
        out["mc_cpi_yoy"].to_numpy(),
        manual.to_numpy(),
        rtol=1e-10,
        equal_nan=True,
    )


# ─── anti-leakage ───────────────────────────────────────────
def test_anti_leakage_future_macro_poisoned(
    settings: FeatureSettings,
) -> None:
    """Poisoning macro values after cutoff must not change features
    at or before cutoff."""
    df = _prices_df(n=500)
    macro = _macro_df(n=500)
    clean = add_macro_features(df, macro, settings=settings)

    cutoff_idx = 350
    poisoned = macro.copy()
    rng = np.random.default_rng(0)
    n_future = len(poisoned) - cutoff_idx - 1
    for col in [
        "macro_fedfunds",
        "macro_dgs10",
        "macro_dgs2",
        "macro_cpiaucsl",
        "macro_payems",
        "macro_unrate",
    ]:
        poisoned.loc[cutoff_idx + 1 :, col] = rng.uniform(1e3, 1e4, size=n_future)

    after = add_macro_features(df, poisoned, settings=settings)

    past = slice(0, cutoff_idx + 1)
    for col in macro_feature_columns(settings):
        np.testing.assert_array_equal(
            clean[col].iloc[past].to_numpy(),
            after[col].iloc[past].to_numpy(),
            err_msg=f"feature {col} changed when future macro was poisoned",
        )
