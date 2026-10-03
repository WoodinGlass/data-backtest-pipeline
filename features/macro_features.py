"""Macro point-in-time features.

Six macro features are joined to the (ticker, trade_date) frame from
``marts.fct_macro_daily`` — the wide macro table built in M3.6 that
is already vintage-aware (PIT-correct). We only select and rename;
no additional look-ahead is possible.

Features:

    mc_fedfunds     Fed Funds Effective Rate (percent)
    mc_dgs10        10-Year Treasury Yield (percent)
    mc_dgs2         2-Year Treasury Yield (percent)
    mc_cpi_yoy      CPI All Urban (SA) year-over-year (% change, 12mo lag)
    mc_payems_yoy   Nonfarm Payrolls year-over-year (% change, 12mo lag)
    mc_unrate       Unemployment Rate (percent)

The YoY transformations use a 252-trading-day lag on the forward-
filled daily series (approximately 12 calendar months). This is the
standard convention when working with daily panels of macro data.

Inputs (columns required in ``df``):

    ticker       str
    trade_date   date or datetime

Inputs (columns required in ``macro_df``):

    trade_date          date or datetime
    macro_fedfunds      float
    macro_dgs10         float
    macro_dgs2          float
    macro_cpiaucsl      float
    macro_payems        float
    macro_unrate        float

The macro table is joined on ``trade_date`` with a LEFT join, so
tickers retain all their price dates even if a macro value is
missing (in which case the macro feature is NaN).
"""

from __future__ import annotations

import pandas as pd

from features.config import FeatureSettings, get_feature_settings

__all__ = [
    "MACRO_SOURCE_COLUMNS",
    "add_macro_features",
    "macro_feature_columns",
]


# Source columns in marts.fct_macro_daily -> output feature name
MACRO_SOURCE_COLUMNS: dict[str, str] = {
    "macro_fedfunds": "mc_fedfunds",
    "macro_dgs10": "mc_dgs10",
    "macro_dgs2": "mc_dgs2",
    "macro_cpiaucsl": "mc_cpi_yoy",  # computed from this
    "macro_payems": "mc_payems_yoy",  # computed from this
    "macro_unrate": "mc_unrate",
}

# Trading days per year (used for YoY lag on daily ffill series)
_TRADING_DAYS_PER_YEAR = 252


def macro_feature_columns(
    settings: FeatureSettings | None = None,
) -> tuple[str, ...]:
    """Return the ordered list of macro feature names."""
    _ = settings  # reserved for future window params
    return (
        "mc_fedfunds",
        "mc_dgs10",
        "mc_dgs2",
        "mc_cpi_yoy",
        "mc_payems_yoy",
        "mc_unrate",
    )


def add_macro_features(
    df: pd.DataFrame,
    macro_df: pd.DataFrame,
    *,
    settings: FeatureSettings | None = None,
) -> pd.DataFrame:
    """Return a copy of ``df`` with macro features joined.

    Args:
        df: Frame with ``ticker`` and ``trade_date``.
        macro_df: Wide macro frame with ``trade_date`` and the source
            columns listed in :data:`MACRO_SOURCE_COLUMNS`.
        settings: Optional FeatureSettings override.

    Returns:
        New DataFrame with macro features added.

    Raises:
        ValueError: if required columns are missing.
    """
    settings = settings or get_feature_settings()
    _ = settings  # reserved

    if "ticker" not in df.columns or "trade_date" not in df.columns:
        raise ValueError("add_macro_features: df must have 'ticker' and 'trade_date'")
    if "trade_date" not in macro_df.columns:
        raise ValueError("add_macro_features: macro_df must have 'trade_date'")

    missing = set(MACRO_SOURCE_COLUMNS) - set(macro_df.columns)
    if missing:
        raise ValueError(f"add_macro_features: macro_df missing columns {sorted(missing)}")

    # De-duplicate on trade_date to make the merge safe.
    macro = (
        macro_df.drop_duplicates(subset=["trade_date"])
        .sort_values("trade_date")
        .reset_index(drop=True)
    )

    # Computed YoY columns (percent change vs 252 trading days ago).
    cpi = macro["macro_cpiaucsl"].astype("float64")
    macro["mc_cpi_yoy"] = (cpi / cpi.shift(_TRADING_DAYS_PER_YEAR) - 1.0) * 100.0

    payems = macro["macro_payems"].astype("float64")
    macro["mc_payems_yoy"] = (payems / payems.shift(_TRADING_DAYS_PER_YEAR) - 1.0) * 100.0

    # Direct passthrough for the rate features.
    macro["mc_fedfunds"] = macro["macro_fedfunds"].astype("float64")
    macro["mc_dgs10"] = macro["macro_dgs10"].astype("float64")
    macro["mc_dgs2"] = macro["macro_dgs2"].astype("float64")
    macro["mc_unrate"] = macro["macro_unrate"].astype("float64")

    keep = ["trade_date", *macro_feature_columns()]
    macro_selected = macro[keep]

    out = df.merge(macro_selected, on="trade_date", how="left")

    # Ensure feature dtypes are float64 (defensive against pandas upcast)
    for col in macro_feature_columns():
        out[col] = out[col].astype("float64")

    # Guard: no negative payroll growth, no cpi_yoy below -100%
    # (these are sanity bounds, not invariants we assert on)
    return out
