"""Unit tests for features/market_features.py.

Market context features (beta, correlation vs benchmark). Includes
anti-leakage: poisoning the benchmark returns after time T must not
change market features at T.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from features.config import FeatureSettings
from features.market_features import (
    add_market_context_features,
    market_feature_columns,
)


def _make_df(
    *,
    n: int = 200,
    seed: int = 0,
    include_benchmark: bool = True,
    tickers: tuple[str, ...] = ("AAPL", "MSFT"),
    benchmark: str = "SPY",
) -> pd.DataFrame:
    """Synthetic frame: `tickers` + optional benchmark row per date."""
    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(n)]
    all_tickers = list(tickers) + ([benchmark] if include_benchmark else [])
    rows: list[dict[str, object]] = []
    for tkr in all_tickers:
        rng = np.random.default_rng((hash(tkr) + seed) % (2**31))
        log_ret = rng.normal(0.0, 0.01, size=n)
        for i, d in enumerate(dates):
            rows.append({"ticker": tkr, "trade_date": d, "log_return": float(log_ret[i])})
    return pd.DataFrame(rows)


@pytest.fixture
def settings() -> FeatureSettings:
    return FeatureSettings(window_long=10, benchmark_ticker="SPY")


# ─── contract ───────────────────────────────────────────────
def test_missing_columns_raises(settings: FeatureSettings) -> None:
    df = pd.DataFrame({"foo": [1]})
    with pytest.raises(ValueError, match="missing columns"):
        add_market_context_features(df, settings=settings)


def test_missing_benchmark_raises(settings: FeatureSettings) -> None:
    df = _make_df(include_benchmark=False)
    with pytest.raises(ValueError, match="benchmark ticker"):
        add_market_context_features(df, settings=settings)


def test_returns_all_declared_features(settings: FeatureSettings) -> None:
    out = add_market_context_features(_make_df(), settings=settings)
    for col in market_feature_columns(settings):
        assert col in out.columns, f"missing {col}"


def test_preserves_row_count(settings: FeatureSettings) -> None:
    df = _make_df()
    out = add_market_context_features(df, settings=settings)
    assert len(out) == len(df)


def test_drops_benchmark_helper_column(settings: FeatureSettings) -> None:
    """Regression: benchmark_log_return is internal; must not leak."""
    out = add_market_context_features(_make_df(), settings=settings)
    assert "benchmark_log_return" not in out.columns


def test_output_sorted_by_ticker_then_date(settings: FeatureSettings) -> None:
    df = _make_df(tickers=("MSFT", "AAPL"))
    out = add_market_context_features(df, settings=settings)
    keys = list(zip(out["ticker"], out["trade_date"], strict=True))
    assert keys == sorted(keys)


# ─── self-reference ─────────────────────────────────────────
def test_spy_beta_equals_one(settings: FeatureSettings) -> None:
    out = add_market_context_features(_make_df(n=200), settings=settings)
    spy = out[(out["ticker"] == "SPY") & out[f"mkt_beta_{settings.window_long}d"].notna()]
    assert (spy[f"mkt_beta_{settings.window_long}d"] - 1.0).abs().max() < 1e-9


def test_spy_corr_equals_one(settings: FeatureSettings) -> None:
    out = add_market_context_features(_make_df(n=200), settings=settings)
    spy = out[(out["ticker"] == "SPY") & out[f"mkt_corr_{settings.window_long}d"].notna()]
    assert (spy[f"mkt_corr_{settings.window_long}d"] - 1.0).abs().max() < 1e-9


# ─── boundary NaN ───────────────────────────────────────────
def test_boundary_nan(settings: FeatureSettings) -> None:
    """Rows 0..w-2 have NaN beta/corr; row w-1 is first valid.

    Pandas rolling(window=w, min_periods=w) yields the first value at
    index w-1 (0-indexed), because the window spans indices [0, w-1].
    """
    out = add_market_context_features(_make_df(n=50), settings=settings)
    w = settings.window_long
    for tkr in ("AAPL", "MSFT"):
        sub = out[out["ticker"] == tkr].reset_index(drop=True)
        beta_col = f"mkt_beta_{w}d"
        assert sub[beta_col].iloc[: w - 1].isna().all()
        assert sub[beta_col].iloc[w - 1 :].notna().all()


# ─── correlation range ──────────────────────────────────────
def test_correlation_in_minus_one_to_one(settings: FeatureSettings) -> None:
    out = add_market_context_features(_make_df(n=200), settings=settings)
    corr = out[f"mkt_corr_{settings.window_long}d"].dropna()
    assert (corr >= -1.0).all()
    assert (corr <= 1.0).all()


# ─── groupby isolation ──────────────────────────────────────
def test_features_isolated_per_ticker(settings: FeatureSettings) -> None:
    """Adding a second non-benchmark ticker must not change features
    for the first."""
    only_aapl = _make_df(n=100, tickers=("AAPL",))
    out_single = add_market_context_features(only_aapl, settings=settings)
    aapl_single = (
        out_single[out_single["ticker"] == "AAPL"].sort_values("trade_date").reset_index(drop=True)
    )

    aapl_plus_msft = _make_df(n=100, tickers=("AAPL", "MSFT"))
    out_combined = add_market_context_features(aapl_plus_msft, settings=settings)
    aapl_combined = (
        out_combined[out_combined["ticker"] == "AAPL"]
        .sort_values("trade_date")
        .reset_index(drop=True)
    )

    for col in market_feature_columns(settings):
        np.testing.assert_array_equal(
            aapl_single[col].to_numpy(),
            aapl_combined[col].to_numpy(),
            err_msg=f"{col} changed when MSFT was added",
        )


# ─── anti-leakage ───────────────────────────────────────────
def test_anti_leakage_benchmark_poisoned(settings: FeatureSettings) -> None:
    """Poisoning benchmark returns after T must not change features at T."""
    df = _make_df(n=200)
    clean = add_market_context_features(df, settings=settings)

    cutoff_idx = 120
    cutoff_date = sorted(df["trade_date"].unique())[cutoff_idx]

    poisoned = df.copy()
    bm_mask = (poisoned["ticker"] == "SPY") & (poisoned["trade_date"] > cutoff_date)
    rng = np.random.default_rng(0)
    poisoned.loc[bm_mask, "log_return"] = rng.normal(0.0, 1.0, size=bm_mask.sum())

    after = add_market_context_features(poisoned, settings=settings)

    past = clean["trade_date"] <= cutoff_date
    for col in market_feature_columns(settings):
        np.testing.assert_array_equal(
            clean[col][past].to_numpy(),
            after[col][past].to_numpy(),
            err_msg=f"{col} changed when future benchmark was poisoned",
        )


def test_anti_leakage_stock_poisoned(settings: FeatureSettings) -> None:
    """Poisoning a stock's own returns after T must not change its
    features at T (and must not change other stocks' features at T)."""
    df = _make_df(n=200)
    clean = add_market_context_features(df, settings=settings)

    cutoff_idx = 120
    cutoff_date = sorted(df["trade_date"].unique())[cutoff_idx]

    poisoned = df.copy()
    mask = (poisoned["ticker"] == "AAPL") & (poisoned["trade_date"] > cutoff_date)
    rng = np.random.default_rng(1)
    poisoned.loc[mask, "log_return"] = rng.normal(0.0, 1.0, size=mask.sum())

    after = add_market_context_features(poisoned, settings=settings)

    past = clean["trade_date"] <= cutoff_date
    for col in market_feature_columns(settings):
        np.testing.assert_array_equal(
            clean[col][past].to_numpy(),
            after[col][past].to_numpy(),
            err_msg=f"{col} changed when future AAPL was poisoned",
        )
