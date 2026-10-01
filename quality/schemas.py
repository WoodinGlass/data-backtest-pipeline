"""Pandera schemas for the data quality gate.

One schema per layer boundary. Each is a declarative contract:
column types, nullability, value ranges, and (via dataframe checks)
row-level invariants that span multiple columns.

Design notes
------------
- Import path is `pandera.pandas as pa` (0.20+ style). Pandera 0.33
  deprecates top-level `import pandera as pa`; the new path is stable.
- `Config.strict = True`: any unexpected column fails. Schema drift
  should be loud, not silent.
- `Config.coerce = True`: allow DuckDB/Pandas to hand us datetime or
  numeric types with minor variations, then normalise.
- Row-level invariants use `@dataframe_check` returning a boolean
  Series: False where a row violates the contract. Pandera aggregates
  and raises a detailed error.

Relation to other checks
------------------------
- `ingestion/validation.py`: fast vectorized checks in the ingestion
  hot path (before Parquet is written).
- `quality/schemas.py` (this file): declarative contracts at layer
  boundaries (raw, staging, marts). Runs in CI and in the gate CLI.
- `dbt/models/**/*.yml`: SQL-level tests in the warehouse.

These are three defense layers, not three copies of the same code.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
from pandera.pandas import DataFrameModel, Field, dataframe_check
from pandera.typing import Series

__all__ = [
    "SCHEMAS",
    "FctReturnsSchema",
    "RawPricesSchema",
    "StgPricesSchema",
]


# ═══════════════════════════════════════════════════════════
# Raw layer
# ═══════════════════════════════════════════════════════════
class RawPricesSchema(DataFrameModel):
    """Contract for the immutable raw Parquet layer.

    Columns exactly match what RawStore.write_snapshot writes: a
    `ticker` column added at write time, plus OHLCV. No derived
    columns (e.g. no returns, no sector).
    """

    ticker: Series[str] = Field(
        description="Uppercase ticker symbol",
        str_matches=r"^[A-Z][A-Z0-9]{0,9}$",
    )
    date: Series[datetime] = Field(
        description="Trade date (UTC-naive)",
    )
    open: Series[float] = Field(gt=0, description="Open price")
    high: Series[float] = Field(gt=0, description="High price")
    low: Series[float] = Field(gt=0, description="Low price")
    close: Series[float] = Field(gt=0, description="Close price")
    adj_close: Series[float] = Field(gt=0, description="Adjusted close")
    volume: Series[int] = Field(ge=0, description="Share volume")

    class Config:
        strict = True
        coerce = True
        ordered = False

    @dataframe_check
    @classmethod
    def ohlc_invariants(cls, df: pd.DataFrame) -> pd.Series:
        """low <= open <= high and low <= close <= high."""
        return (
            (df["low"] <= df["open"])
            & (df["open"] <= df["high"])
            & (df["low"] <= df["close"])
            & (df["close"] <= df["high"])
        )


# ═══════════════════════════════════════════════════════════
# Staging layer
# ═══════════════════════════════════════════════════════════
class StgPricesSchema(DataFrameModel):
    """Contract for `staging.stg_prices`.

    Same columns as raw, but `date` is renamed to `trade_date` and
    types are guaranteed by the SQL casts in stg_prices.sql.
    """

    ticker: Series[str] = Field(str_matches=r"^[A-Z][A-Z0-9]{0,9}$")
    trade_date: Series[datetime] = Field(description="Trade date")
    open: Series[float] = Field(gt=0)
    high: Series[float] = Field(gt=0)
    low: Series[float] = Field(gt=0)
    close: Series[float] = Field(gt=0)
    adj_close: Series[float] = Field(gt=0)
    volume: Series[int] = Field(ge=0)

    class Config:
        strict = True
        coerce = True
        ordered = False

    @dataframe_check
    @classmethod
    def ohlc_invariants(cls, df: pd.DataFrame) -> pd.Series:
        return (
            (df["low"] <= df["open"])
            & (df["open"] <= df["high"])
            & (df["low"] <= df["close"])
            & (df["close"] <= df["high"])
        )


# ═══════════════════════════════════════════════════════════
# Marts layer
# ═══════════════════════════════════════════════════════════
class FctReturnsSchema(DataFrameModel):
    """Contract for `marts.fct_returns_daily`.

    The model-ready table: features known at t plus labels known at
    t+1. Nullability mirrors the point-in-time boundaries established
    in int_returns (first/last bar per ticker).
    """

    ticker: Series[str] = Field(str_matches=r"^[A-Z][A-Z0-9]{0,9}$")
    trade_date: Series[datetime]

    # Features known at end of t.
    close: Series[float] = Field(gt=0)
    adj_close: Series[float] = Field(gt=0)
    volume: Series[int] = Field(ge=0)
    log_return: Series[float] = Field(
        nullable=True,
        description="NULL on first bar per ticker",
    )

    # Labels known at end of t+1.
    next_trade_date: Series[datetime] = Field(nullable=True)
    next_close: Series[float] = Field(nullable=True, gt=0)
    next_adj_close: Series[float] = Field(nullable=True, gt=0)
    next_log_return: Series[float] = Field(nullable=True)
    next_return_positive: Series[bool] = Field(nullable=True)

    # Dimension attributes.
    sector: Series[str] = Field(str_length={"min_value": 1})
    is_benchmark: Series[bool]

    class Config:
        strict = True
        coerce = True
        ordered = False

    @dataframe_check
    @classmethod
    def log_return_range(cls, df: pd.DataFrame) -> pd.Series:
        """|log_return| < 1 (NULL allowed on first bar)."""
        return df["log_return"].isna() | (df["log_return"].abs() < 1.0)

    @dataframe_check
    @classmethod
    def next_log_return_range(cls, df: pd.DataFrame) -> pd.Series:
        return df["next_log_return"].isna() | (df["next_log_return"].abs() < 1.0)

    @dataframe_check
    @classmethod
    def label_null_equivalence(cls, df: pd.DataFrame) -> pd.Series:
        """next_return_positive is NULL iff next_log_return is NULL."""
        return df["next_return_positive"].isna() == df["next_log_return"].isna()

    @dataframe_check
    @classmethod
    def forward_dates_ordered(cls, df: pd.DataFrame) -> pd.Series:
        """next_trade_date must be strictly after trade_date."""
        ntd = df["next_trade_date"]
        return ntd.isna() | (ntd > df["trade_date"])


# ═══════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════
# Name -> schema class. The gate and CLI iterate over this; adding a
# new layer means adding one entry here, not touching gate logic.
SCHEMAS: dict[str, type[DataFrameModel]] = {
    "raw": RawPricesSchema,
    "staging": StgPricesSchema,
    "marts": FctReturnsSchema,
}
