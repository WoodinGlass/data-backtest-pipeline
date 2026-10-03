"""Fundamental point-in-time features.

Five fundamental features are computed from the long-format SEC facts
table ``intermediate.int_fundamentals_pit`` (built in M3.8). That
table is filing-date PIT-correct: for every (ticker, trade_date) the
value reflects the latest formal filing whose ``filed`` date is on or
before ``trade_date``.

The features:

    fd_net_margin         Net income / revenue
    fd_roe                Net income / stockholders' equity
    fd_capex_intensity    Capex / revenue
    fd_rev_growth_yoy     Revenue growth, year-over-year (252 trading days)
    fd_employees          Employee count (best-effort coverage)

Coverage caveats
----------------

- **Tag migration (ASC 606).** Revenue is reported under ``Revenues``
  before 2018 and under ``RevenueFromContractWithCustomerExcludingAssessedTax``
  after. Both are coalesced into a single ``revenue_used`` column.

- **Mixed filing frequency.** A daily row might reflect a quarterly
  (10-Q) or annual (10-K) filing depending on which is more recent.
  Ratios like margin, ROE, and capex intensity are roughly stable
  across both. Year-over-year growth on the daily ffill series uses a
  252-trading-day lag; the caller can filter ``int_fund_df`` to
  annual filings only if consistency matters more than currency.

- **NULL is meaningful.** A missing fundamental feature means the
  company has not yet filed a fact for that tag (or the tag does not
  apply to its sector). We do not forward-fill from the future.

Inputs (columns required in ``int_fund_df``):

    ticker        str
    trade_date    date or datetime
    tag           str   (XBRL tag name)
    value         float

Inputs (columns required in ``df``):

    ticker        str
    trade_date    date or datetime
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from features.config import FeatureSettings, get_feature_settings

__all__ = [
    "FUNDAMENTAL_TAGS",
    "add_fundamental_features",
    "fundamental_feature_columns",
]


# XBRL tags grouped by logical concept. Multiple tags per concept are
# coalesced (first non-null wins).
FUNDAMENTAL_TAGS: dict[str, tuple[str, ...]] = {
    "net_income": ("NetIncomeLoss",),
    "revenue": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    "equity": ("StockholdersEquity",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
    "employees": ("EntityNumberOfEmployees", "NumberOfEmployees"),
}

_TRADING_DAYS_PER_YEAR = 252


def fundamental_feature_columns(
    settings: FeatureSettings | None = None,
) -> tuple[str, ...]:
    """Return the ordered list of fundamental feature names."""
    _ = settings
    return (
        "fd_net_margin",
        "fd_roe",
        "fd_capex_intensity",
        "fd_rev_growth_yoy",
        "fd_employees",
    )


def _all_tags() -> set[str]:
    out: set[str] = set()
    for tags in FUNDAMENTAL_TAGS.values():
        out.update(tags)
    return out


def add_fundamental_features(
    df: pd.DataFrame,
    int_fund_df: pd.DataFrame,
    *,
    settings: FeatureSettings | None = None,
) -> pd.DataFrame:
    """Return a copy of ``df`` with fundamental features joined.

    Args:
        df: Frame with ``ticker`` and ``trade_date``.
        int_fund_df: Long-format facts from ``int_fundamentals_pit``
            (or any equivalent long table with ``ticker``,
            ``trade_date``, ``tag``, ``value``).
        settings: Optional FeatureSettings override.

    Returns:
        New DataFrame with the ``fd_*`` columns added.

    Raises:
        ValueError: if required columns are missing.
    """
    settings = settings or get_feature_settings()
    _ = settings

    for col in ("ticker", "trade_date"):
        if col not in df.columns:
            raise ValueError(f"add_fundamental_features: df missing '{col}'")
    for col in ("ticker", "trade_date", "tag", "value"):
        if col not in int_fund_df.columns:
            raise ValueError(f"add_fundamental_features: int_fund_df missing '{col}'")

    needed = _all_tags()
    sub = int_fund_df.loc[int_fund_df["tag"].isin(needed)].copy()
    if sub.empty:
        # No matching facts: produce all-NaN columns so downstream
        # pipelines still get a full schema.
        out = df.copy()
        for col in fundamental_feature_columns():
            out[col] = np.nan
        return out

    # Pivot long -> wide: one row per (ticker, trade_date).
    pivot = sub.pivot_table(
        index=["ticker", "trade_date"],
        columns="tag",
        values="value",
        aggfunc="first",
    ).reset_index()

    def _series(col: str) -> pd.Series[float]:
        if col in pivot.columns:
            return pivot[col].astype("float64")
        return pd.Series(np.nan, index=pivot.index, dtype="float64")

    # Coalesce revenue tags (pre- vs post-ASC 606).
    rev_cols = [c for c in FUNDAMENTAL_TAGS["revenue"] if c in pivot.columns]
    if rev_cols:
        revenue_used = pivot[rev_cols].bfill(axis=1).iloc[:, 0].astype("float64")
    else:
        revenue_used = pd.Series(np.nan, index=pivot.index, dtype="float64")

    emp_cols = [c for c in FUNDAMENTAL_TAGS["employees"] if c in pivot.columns]
    if emp_cols:
        employees_used = pivot[emp_cols].bfill(axis=1).iloc[:, 0].astype("float64")
    else:
        employees_used = pd.Series(np.nan, index=pivot.index, dtype="float64")

    net_income = _series("NetIncomeLoss")
    equity = _series("StockholdersEquity")
    capex = _series("PaymentsToAcquirePropertyPlantAndEquipment")

    with np.errstate(divide="ignore", invalid="ignore"):
        pivot["fd_net_margin"] = (net_income / revenue_used).replace([np.inf, -np.inf], np.nan)
        pivot["fd_roe"] = (net_income / equity).replace([np.inf, -np.inf], np.nan)
        pivot["fd_capex_intensity"] = (capex / revenue_used).replace([np.inf, -np.inf], np.nan)

    pivot["fd_employees"] = employees_used

    # Year-over-year revenue growth via 252-trading-day lag per ticker.
    pivot = pivot.sort_values(["ticker", "trade_date"]).reset_index(drop=True)
    pivot["_revenue_used"] = revenue_used.values
    pivot["fd_rev_growth_yoy"] = pivot.groupby("ticker", sort=False)["_revenue_used"].transform(
        lambda s: s / s.shift(_TRADING_DAYS_PER_YEAR) - 1.0
    )

    # Sanity clamps: ratios can explode when denominator is tiny.
    # Clamp to a generous [-5, +5] for margins/ROE and [0, 2] for capex
    # intensity. These are bounds, not invariants.
    pivot["fd_net_margin"] = pivot["fd_net_margin"].clip(-5.0, 5.0)
    pivot["fd_roe"] = pivot["fd_roe"].clip(-5.0, 5.0)
    pivot["fd_capex_intensity"] = pivot["fd_capex_intensity"].clip(0.0, 2.0)
    pivot["fd_rev_growth_yoy"] = pivot["fd_rev_growth_yoy"].clip(-1.0, 5.0)

    cols_out = ["ticker", "trade_date", *fundamental_feature_columns()]
    features_df = pivot[cols_out]

    out = df.merge(features_df, on=["ticker", "trade_date"], how="left")
    for col in fundamental_feature_columns():
        out[col] = out[col].astype("float64")
    return out
