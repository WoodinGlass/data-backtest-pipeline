"""Market context features.

For each (ticker, trade_date), measure how the ticker co-moves with
the market (benchmark = SPY by default) over a rolling window:

    mkt_beta_{N}d   = cov(stock_ret, market_ret) / var(market_ret)
    mkt_corr_{N}d   = corr(stock_ret, market_ret)

Both use ``log_return`` from ``marts.fct_returns_daily`` and the
benchmark return at the same trade_date. Rolling windows of length
``window_long`` (60 by default). No look-ahead: the window at t
includes only t and the N-1 bars before t.

Self-reference: for the benchmark ticker itself, beta = 1 and
corr = 1 by construction.

Inputs (columns required):

    ticker       str
    trade_date   date or datetime
    log_return   float

The benchmark ticker (default ``SPY``) must be present in ``df``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from features.config import FeatureSettings, get_feature_settings

__all__ = [
    "add_market_context_features",
    "market_feature_columns",
]


def market_feature_columns(
    settings: FeatureSettings | None = None,
) -> tuple[str, ...]:
    """Return the ordered list of market context feature names."""
    s = settings or get_feature_settings()
    n = s.window_long
    return (
        f"mkt_beta_{n}d",
        f"mkt_corr_{n}d",
    )


def add_market_context_features(
    df: pd.DataFrame,
    *,
    benchmark_ticker: str | None = None,
    settings: FeatureSettings | None = None,
) -> pd.DataFrame:
    """Return a copy of ``df`` with market context features added."""
    settings = settings or get_feature_settings()
    bm = benchmark_ticker or settings.benchmark_ticker
    w = settings.window_long

    required = {"ticker", "trade_date", "log_return"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"add_market_context_features: missing columns {sorted(missing)}")

    bench = df.loc[df["ticker"] == bm, ["trade_date", "log_return"]]
    if bench.empty:
        raise ValueError(f"benchmark ticker {bm!r} not present in df")
    bench = bench.rename(columns={"log_return": "benchmark_log_return"})

    out = df.merge(bench, on="trade_date", how="left")
    out = out.sort_values(["ticker", "trade_date"]).reset_index(drop=True)

    beta_col = f"mkt_beta_{w}d"
    corr_col = f"mkt_corr_{w}d"

    def _compute(group: pd.DataFrame) -> pd.DataFrame:
        x = group["log_return"]
        y = group["benchmark_log_return"]
        cov = x.rolling(w, min_periods=w).cov(y)
        var = y.rolling(w, min_periods=w).var()
        with np.errstate(divide="ignore", invalid="ignore"):
            beta = cov / var
        corr = x.rolling(w, min_periods=w).corr(y).clip(-1.0, 1.0)
        return pd.DataFrame(
            {
                beta_col: beta.replace([np.inf, -np.inf], np.nan),
                corr_col: corr,
            },
            index=group.index,
        )

    computed = out.groupby("ticker", sort=False, group_keys=False).apply(_compute)
    out[beta_col] = computed[beta_col].astype("float64")
    out[corr_col] = computed[corr_col].astype("float64")
    return out
