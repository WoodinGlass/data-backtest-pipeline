"""Cross-sectional point-in-time features.

These features rank a ticker against its peers **on the same
trade_date**, using only data available at the close of that date.

Two ranks are provided:

    cs_sector_rank_{N}d   = within-sector percentile rank of px_ret_{N}d
    cs_market_rank_{N}d   = cross-universe percentile rank of px_ret_{N}d

Both are percentile ranks in [0, 1] computed with pandas ``rank(pct=True)``.
They are the natural input to a cross-sectional model: "is this ticker
stronger than its peers today?".

Design (ADR 0012):

- **Pure function.** Input DataFrame in, output DataFrame out.
- **PIT-safe.** Every rank uses data from a single trade_date only;
  no time-series lag or lead, no look-ahead.
- **NaN-safe.** Rows with NaN ``px_ret_{N}d`` (typically the first N
  bars per ticker) get NaN rank, not a spurious middle rank.

Inputs (columns required):

    ticker       str
    trade_date   date or datetime
    sector       str    (from dim_tickers via fct_returns_daily)
    px_ret_{N}d  float  (from add_price_features)

Outputs (columns added):

    cs_sector_rank_{N}d   percentile rank within (trade_date, sector)
    cs_market_rank_{N}d   percentile rank within (trade_date)
"""

from __future__ import annotations

import pandas as pd

from features.config import FeatureSettings, get_feature_settings

__all__ = [
    "add_cross_sectional_features",
    "cross_sectional_feature_columns",
]


def cross_sectional_feature_columns(
    settings: FeatureSettings | None = None,
) -> tuple[str, ...]:
    """Return the ordered list of cross-sectional feature names."""
    s = settings or get_feature_settings()
    n = s.window_medium
    return (
        f"cs_sector_rank_{n}d",
        f"cs_market_rank_{n}d",
    )


def add_cross_sectional_features(
    df: pd.DataFrame,
    *,
    settings: FeatureSettings | None = None,
) -> pd.DataFrame:
    """Return a copy of ``df`` with cross-sectional ranks added.

    Args:
        df: Frame with at least ``ticker``, ``trade_date``, ``sector``,
            and a price-return column for the medium window
            (``px_ret_{window_medium}d``).
        settings: Optional FeatureSettings override.

    Returns:
        New DataFrame with the ``cs_*`` columns added.

    Raises:
        ValueError: if required columns are missing.
    """
    settings = settings or get_feature_settings()
    n = settings.window_medium
    ret_col = f"px_ret_{n}d"

    required = {"ticker", "trade_date", "sector", ret_col}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"add_cross_sectional_features: missing columns {sorted(missing)}")

    out = df.copy()

    # Market rank: percentile within each trade_date across all tickers.
    # NaN returns stay NaN (rank with na_option="keep").
    out[f"cs_market_rank_{n}d"] = (
        out.groupby("trade_date")[ret_col].rank(pct=True, na_option="keep").astype("float64")
    )

    # Sector rank: percentile within each (trade_date, sector).
    out[f"cs_sector_rank_{n}d"] = (
        out.groupby(["trade_date", "sector"])[ret_col]
        .rank(pct=True, na_option="keep")
        .astype("float64")
    )

    return out
