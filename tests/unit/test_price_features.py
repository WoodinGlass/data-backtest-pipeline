"""Unit tests for features/price_features.py.

Includes the anti-leakage test that defines this module's contract:
features at time t must not depend on any data from t+1 or later.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from features.config import FeatureSettings
from features.price_features import (
    _rsi,
    add_price_features,
    price_feature_columns,
)


def _make_df(
    *,
    ticker: str = "AAPL",
    n: int = 100,
    start: date = date(2024, 1, 1),
    seed: int = 0,
) -> pd.DataFrame:
    """Synthetic frame with adj_close + log_return for one ticker."""
    rng = np.random.default_rng(seed)
    dates = [start + timedelta(days=i) for i in range(n)]
    log_ret = rng.normal(0.0, 0.01, size=n).astype("float64")
    log_ret[0] = np.nan
    adj_close = 100.0 * np.exp(np.cumsum(np.nan_to_num(log_ret)))
    return pd.DataFrame(
        {
            "ticker": [ticker] * n,
            "trade_date": dates,
            "adj_close": adj_close.astype("float64"),
            "log_return": log_ret,
        }
    )


@pytest.fixture
def settings() -> FeatureSettings:
    return FeatureSettings(
        window_short=3,
        window_medium=5,
        window_long=10,
        window_rsi=5,
        trading_days_per_year=252,
    )


# ─── contract ───────────────────────────────────────────────
def test_missing_column_raises(settings: FeatureSettings) -> None:
    df = _make_df().drop(columns=["adj_close"])
    with pytest.raises(ValueError, match="missing columns"):
        add_price_features(df, settings=settings)


def test_returns_all_declared_features(settings: FeatureSettings) -> None:
    out = add_price_features(_make_df(), settings=settings)
    for col in price_feature_columns(settings):
        assert col in out.columns, f"missing {col}"


def test_does_not_mutate_input(settings: FeatureSettings) -> None:
    df = _make_df()
    before = df.copy(deep=True)
    _ = add_price_features(df, settings=settings)
    pd.testing.assert_frame_equal(df, before)


def test_output_sorted_by_ticker_then_date(settings: FeatureSettings) -> None:
    df_a = _make_df(ticker="AAPL", n=30, seed=1)
    df_m = _make_df(ticker="MSFT", n=30, seed=2)
    combined = pd.concat([df_m, df_a], ignore_index=True)
    out = add_price_features(combined, settings=settings)
    keys = list(zip(out["ticker"], out["trade_date"], strict=True))
    assert keys == sorted(keys)


# ─── per-feature ────────────────────────────────────────────
def test_px_ret_1d_equals_log_return(settings: FeatureSettings) -> None:
    out = add_price_features(_make_df(n=50), settings=settings)
    np.testing.assert_array_equal(
        out["px_ret_1d"].to_numpy(),
        out["log_return"].to_numpy(),
    )


def test_px_ret_short_matches_manual_calc(settings: FeatureSettings) -> None:
    df = _make_df(n=30, seed=3)
    out = add_price_features(df, settings=settings)
    w = settings.window_short
    manual = np.log(out["adj_close"] / out["adj_close"].shift(w))
    col = f"px_ret_{w}d"
    pd.testing.assert_series_equal(
        out[col].iloc[w:].reset_index(drop=True),
        manual.iloc[w:].reset_index(drop=True),
        check_names=False,
    )


def test_px_vol_boundary_is_nan(settings: FeatureSettings) -> None:
    out = add_price_features(_make_df(n=30), settings=settings)
    col = f"px_vol_{settings.window_long}d"
    assert out[col].iloc[: settings.window_long].isna().all()
    assert out[col].iloc[settings.window_long :].notna().all()


def test_px_rsi_bounded_0_100(settings: FeatureSettings) -> None:
    out = add_price_features(_make_df(n=100, seed=42), settings=settings)
    rsi = out[f"px_rsi_{settings.window_rsi}"].dropna()
    assert (rsi >= 0).all()
    assert (rsi <= 100).all()


def test_px_vol_ratio_positive_or_nan(settings: FeatureSettings) -> None:
    out = add_price_features(_make_df(n=100, seed=4), settings=settings)
    col = f"px_vol_ratio_{settings.window_short}_{settings.window_medium}"
    ratio = out[col].dropna()
    assert (ratio >= 0).all()


def test_px_mom_lower_bound(settings: FeatureSettings) -> None:
    out = add_price_features(_make_df(n=100, seed=5), settings=settings)
    col = f"px_mom_{settings.window_long}d"
    mom = out[col].dropna()
    assert (mom > -1.0).all()


# ─── groupby safety ─────────────────────────────────────────
def test_features_are_ticker_isolated(settings: FeatureSettings) -> None:
    df_a = _make_df(ticker="AAPL", n=50, seed=1)
    out_alone = add_price_features(df_a, settings=settings)

    df_a_plus_other = pd.concat(
        [df_a, _make_df(ticker="MSFT", n=50, seed=99)],
        ignore_index=True,
    )
    out_combined = add_price_features(df_a_plus_other, settings=settings)
    out_combined_a = (
        out_combined[out_combined["ticker"] == "AAPL"]
        .sort_values("trade_date")
        .reset_index(drop=True)
    )

    for col in price_feature_columns(settings):
        pd.testing.assert_series_equal(
            out_alone[col].reset_index(drop=True),
            out_combined_a[col].reset_index(drop=True),
            check_names=False,
            obj=f"feature {col} differs when ticker is joined by another",
        )


# ─── anti-leakage ───────────────────────────────────────────
@pytest.mark.parametrize("cutoff_idx", [30, 60, 80])
def test_anti_leakage_future_garbage_does_not_change_past(
    cutoff_idx: int,
    settings: FeatureSettings,
) -> None:
    df = _make_df(n=100, seed=7)
    clean = add_price_features(df, settings=settings)

    poisoned = df.copy()
    rng = np.random.default_rng(0)
    n_future = len(poisoned) - cutoff_idx - 1
    poisoned.loc[cutoff_idx + 1 :, "adj_close"] = rng.uniform(1e6, 1e7, size=n_future)
    poisoned.loc[cutoff_idx + 1 :, "log_return"] = rng.normal(0.0, 5.0, size=n_future)

    after = add_price_features(poisoned, settings=settings)

    past = slice(0, cutoff_idx + 1)
    for col in price_feature_columns(settings):
        np.testing.assert_array_equal(
            clean[col].iloc[past].to_numpy(),
            after[col].iloc[past].to_numpy(),
            err_msg=f"feature {col} changed when future rows were poisoned",
        )


def test_anti_leakage_shuffled_future_does_not_change_past(
    settings: FeatureSettings,
) -> None:
    df = _make_df(n=80, seed=11)
    clean = add_price_features(df, settings=settings)

    cutoff_idx = 50
    shuffled = df.copy()
    future = shuffled.iloc[cutoff_idx + 1 :].sample(frac=1.0, random_state=0)
    shuffled.iloc[cutoff_idx + 1 :] = future.to_numpy()

    after = add_price_features(shuffled, settings=settings)

    past = slice(0, cutoff_idx + 1)
    for col in price_feature_columns(settings):
        np.testing.assert_array_equal(
            clean[col].iloc[past].to_numpy(),
            after[col].iloc[past].to_numpy(),
            err_msg=f"feature {col} changed when future rows were shuffled",
        )


# ─── _rsi helper ────────────────────────────────────────────
def test_rsi_monotone_increasing_input_returns_high_value() -> None:
    close = pd.Series(np.arange(1.0, 50.0))
    rsi = _rsi(close, period=5)
    assert rsi.dropna().iloc[-1] > 90.0


def test_rsi_monotone_decreasing_input_returns_low_value() -> None:
    close = pd.Series(np.arange(50.0, 1.0, -1.0))
    rsi = _rsi(close, period=5)
    assert rsi.dropna().iloc[-1] < 10.0
