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
    "IntFundamentalsPitSchema",
    "IntMacroDailySchema",
    "RawPricesSchema",
    "SecFactsSchema",
    "StgMacroSeriesSchema",
    "StgPricesSchema",
    "StgSecFactsSchema",
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
# Raw fundamentals (SEC EDGAR)
# ═══════════════════════════════════════════════════════════
class SecFactsSchema(DataFrameModel):
    """Contract for the raw SEC fundamental facts layer.

    One row per (ticker, namespace, tag, unit, period_end, filed,
    form, frame). Includes the PIT key `filed` — the date the fact
    first appeared in a filing. Nullability mirrors what SEC returns:
    `period_start` is NULL for point-in-time facts (e.g. balance-sheet
    values); `value` can be NULL for discontinued segments.
    """

    ticker: Series[str] = Field(
        str_matches=r"^[A-Z][A-Z0-9]{0,9}$",
        description="Uppercase ticker symbol",
    )
    cik: Series[int] = Field(ge=0, description="SEC Central Index Key")
    namespace: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 16},
    )
    tag: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 256},
    )
    unit: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 32},
    )

    period_start: Series[pd.Timestamp] = Field(nullable=True)
    period_end: Series[pd.Timestamp]
    filed: Series[pd.Timestamp]

    form: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 16},
    )
    fiscal_year: Series[int] = Field(nullable=True)
    fiscal_period: Series[str] = Field(nullable=True)
    frame: Series[str] = Field(nullable=True)
    value: Series[float] = Field(nullable=True)

    class Config:
        strict = True
        coerce = True
        ordered = False

    # NOTE on `filed` vs `period_end`
    # --------------------------------
    # It is tempting to assert `filed >= period_end` at the raw layer.
    # In practice SEC XBRL data legitimately contains facts where
    # `filed < period_end`:
    #   - preliminary 8-K disclosures of an in-progress period,
    #   - forward-looking guidance tagged with a future period_end,
    #   - stub periods and reclassifications.
    # The raw layer mirrors what SEC returned; enforcement of the PIT
    # rule (`filed <= trade_date` at consumption time) belongs in the
    # intermediate layer (`int_fundamentals_pit`, M3.8) as a dbt
    # singular test, not here.

    @dataframe_check
    @classmethod
    def period_start_before_end(cls, df: pd.DataFrame) -> pd.Series:
        """When period_start is present, it must precede period_end."""
        ps = df["period_start"]
        return ps.isna() | (ps <= df["period_end"])


# ═══════════════════════════════════════════════════════════
# Macro staging + intermediate
# ═══════════════════════════════════════════════════════════
class StgMacroSeriesSchema(DataFrameModel):
    """Contract for `staging.stg_macro_series`.

    One row per (series_id, observation_date, vintage_date). For
    latest-mode series, `vintage_date` equals `observation_date`
    (see ADR 0009). `value` may be NULL when FRED returned "." for
    a missing period (e.g. a holiday in a daily series).
    """

    series_id: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 64},
        description="FRED series id",
    )
    observation_date: Series[datetime] = Field(
        description="Period the value describes",
    )
    vintage_date: Series[datetime] = Field(
        description="PIT-effective vintage date",
    )
    value: Series[float] = Field(nullable=True)

    class Config:
        strict = True
        coerce = True
        ordered = False

    # NOTE: no check that `vintage_date >= observation_date`.
    # --------------------------------------------------------
    # It is tempting to assert that a value cannot be known before
    # its period starts. That rule is wrong for projection series
    # like GDPPOT (Real Potential GDP), IORB, IOER: FRED publishes
    # projected values for future periods under a single present-day
    # vintage. On a given vintage (say 2024-03-15) the vintage
    # legitimately carries observations up to 2036.
    #
    # Staging mirrors what FRED returned. The PIT rule that matters
    # is enforced at consumption: `int_macro_daily` requires both
    # `vintage_date <= trade_date` and `observation_date <= trade_date`
    # per (trade_date, series_id). See ADR 0009.


class IntMacroDailySchema(DataFrameModel):
    """Contract for `intermediate.int_macro_daily`.

    One row per (trade_date, series_id). The PIT rule enforced here is
    that both `vintage_date` and `observation_date` are <= `trade_date`:
    a trader on T could only have seen macro data published on or
    before T, and only for periods that had already happened.
    """

    trade_date: Series[datetime] = Field(description="Trading date")
    series_id: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 64},
    )
    vintage_date: Series[datetime] = Field(
        description="Vintage current at trade_date",
    )
    observation_date: Series[datetime] = Field(
        description="Period the value describes",
    )
    value: Series[float] = Field(nullable=True)

    class Config:
        strict = True
        coerce = True
        ordered = False

    @dataframe_check
    @classmethod
    def vintage_not_in_future(cls, df: pd.DataFrame) -> pd.Series:
        """No vintage from after the trade date (no look-ahead)."""
        return df["vintage_date"] <= df["trade_date"]

    @dataframe_check
    @classmethod
    def observation_not_in_future(cls, df: pd.DataFrame) -> pd.Series:
        """No observation from a period that had not happened yet."""
        return df["observation_date"] <= df["trade_date"]


# ═══════════════════════════════════════════════════════════
# SEC staging + intermediate
# ═══════════════════════════════════════════════════════════
class StgSecFactsSchema(DataFrameModel):
    """Contract for `staging.stg_sec_facts`.

    One row per SEC XBRL fact, 1:1 with raw. No tag filtering, no PIT
    filtering: those live in intermediate (ADR 0010). We only require
    the columns a downstream consumer must not be missing.
    """

    ticker: Series[str] = Field(
        str_matches=r"^[A-Z][A-Z0-9]{0,9}$",
    )
    cik: Series[int] = Field(ge=0)
    namespace: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 16},
    )
    tag: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 256},
    )
    unit: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 32},
    )
    period_start: Series[datetime] = Field(nullable=True)
    period_end: Series[datetime]
    filed: Series[datetime]
    form: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 16},
    )
    fiscal_year: Series[int] = Field(nullable=True)
    fiscal_period: Series[str] = Field(nullable=True)
    frame: Series[str] = Field(nullable=True)
    value: Series[float] = Field(nullable=True)

    class Config:
        strict = True
        coerce = True
        ordered = False

    @dataframe_check
    @classmethod
    def period_start_before_end(cls, df: pd.DataFrame) -> pd.Series:
        """When period_start is present, it must not exceed period_end."""
        ps = df["period_start"]
        return ps.isna() | (ps <= df["period_end"])


class IntFundamentalsPitSchema(DataFrameModel):
    """Contract for `intermediate.int_fundamentals_pit`.

    One row per (ticker, trade_date, namespace, tag). The PIT rule:
    every fact used must have been filed on or before trade_date, and
    its period must have ended by that date. `value` is NULL when the
    ticker had not yet filed any fact for that tag.
    """

    trade_date: Series[datetime] = Field(description="Trading date")
    ticker: Series[str] = Field(
        str_matches=r"^[A-Z][A-Z0-9]{0,9}$",
    )
    namespace: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 16},
    )
    tag: Series[str] = Field(
        str_length={"min_value": 1, "max_value": 256},
    )
    value: Series[float] = Field(nullable=True)
    filed_used: Series[datetime] = Field(nullable=True)
    period_end_used: Series[datetime] = Field(nullable=True)

    class Config:
        strict = True
        coerce = True
        ordered = False

    @dataframe_check
    @classmethod
    def filed_not_in_future(cls, df: pd.DataFrame) -> pd.Series:
        """No filing from a date after trade_date."""
        fu = df["filed_used"]
        return fu.isna() | (fu <= df["trade_date"])

    @dataframe_check
    @classmethod
    def period_not_after_filing(cls, df: pd.DataFrame) -> pd.Series:
        """The period must have closed before the fact was filed."""
        pe = df["period_end_used"]
        fu = df["filed_used"]
        return pe.isna() | fu.isna() | (pe <= fu)

    @dataframe_check
    @classmethod
    def period_not_in_future(cls, df: pd.DataFrame) -> pd.Series:
        """The period must have closed before trade_date."""
        pe = df["period_end_used"]
        return pe.isna() | (pe <= df["trade_date"])


# ═══════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════
# Name -> schema class. The gate and CLI iterate over this; adding a
# new layer means adding one entry here, not touching gate logic.
SCHEMAS: dict[str, type[DataFrameModel]] = {
    "raw": RawPricesSchema,
    "staging": StgPricesSchema,
    "marts": FctReturnsSchema,
    "sec": SecFactsSchema,
    "stg_macro": StgMacroSeriesSchema,
    "int_macro": IntMacroDailySchema,
    "stg_sec": StgSecFactsSchema,
    "int_fundamentals": IntFundamentalsPitSchema,
}
