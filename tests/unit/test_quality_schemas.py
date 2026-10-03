"""Unit tests for quality/schemas.py — no external services."""

from datetime import date

import pandas as pd
import pandera.pandas as pa
import pytest

from quality.schemas import (
    SCHEMAS,
    FctReturnsSchema,
    IntFundamentalsPitSchema,
    IntMacroDailySchema,
    RawPricesSchema,
    SecFactsSchema,
    StgMacroSeriesSchema,
    StgPricesSchema,
    StgSecFactsSchema,
)

# ─── constants ──────────────────────────────────────────────
# Pandera 0.33: with lazy=False, both SchemaError (single failure)
# and SchemaErrors (multiple collected failures) can be raised. We
# catch both so tests assert on *what* failed, not on Pandera's
# internal collection strategy.
SCHEMA_ERRORS = (pa.errors.SchemaError, pa.errors.SchemaErrors)


# ─── helpers ────────────────────────────────────────────────
def _raw_df(n: int = 3) -> pd.DataFrame:
    """Well-formed raw prices frame."""
    return pd.DataFrame(
        {
            "ticker": ["AAPL"] * n,
            "date": pd.to_datetime([date(2024, 1, 2 + i) for i in range(n)]),
            "open": [100.0 + i for i in range(n)],
            "high": [102.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [101.0 + i for i in range(n)],
            "adj_close": [101.0 + i for i in range(n)],
            "volume": [1000 * (i + 1) for i in range(n)],
        }
    )


def _stg_df(n: int = 3) -> pd.DataFrame:
    """Well-formed staging frame (date renamed to trade_date)."""
    return _raw_df(n).rename(columns={"date": "trade_date"})


def _marts_df(n: int = 3) -> pd.DataFrame:
    """Well-formed marts frame with exactly the columns of
    fct_returns_daily (no open/high/low; only close/adj_close/volume)."""
    return pd.DataFrame(
        {
            "ticker": ["AAPL"] * n,
            "trade_date": pd.to_datetime([date(2024, 1, 2 + i) for i in range(n)]),
            "close": [101.0 + i for i in range(n)],
            "adj_close": [101.0 + i for i in range(n)],
            "volume": [1000 * (i + 1) for i in range(n)],
            "log_return": [None, -0.005, 0.003][:n],
            "next_trade_date": pd.to_datetime([date(2024, 1, 3 + i) for i in range(n)]),
            "next_close": [101.0 + i for i in range(n)],
            "next_adj_close": [101.0 + i for i in range(n)],
            "next_log_return": [-0.005, 0.003, None][:n],
            "next_return_positive": [False, True, None][:n],
            "sector": ["Information Technology"] * n,
            "is_benchmark": [False] * n,
        }
    )


# ═══════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════
def test_schemas_registry_has_expected_layers() -> None:
    # Eight layers now: raw prices, staging, marts, raw SEC facts,
    # macro staging, macro intermediate, SEC staging, SEC intermediate.
    # Adding a new layer means updating this set and the SCHEMAS dict.
    assert set(SCHEMAS.keys()) == {
        "raw",
        "staging",
        "marts",
        "sec",
        "stg_macro",
        "int_macro",
        "stg_sec",
        "int_fundamentals",
    }


def test_registry_classes_match_imports() -> None:
    assert SCHEMAS["raw"] is RawPricesSchema
    assert SCHEMAS["staging"] is StgPricesSchema
    assert SCHEMAS["marts"] is FctReturnsSchema


# ═══════════════════════════════════════════════════════════
# RawPricesSchema
# ═══════════════════════════════════════════════════════════
def test_raw_valid_frame_passes() -> None:
    RawPricesSchema.validate(_raw_df(), lazy=True)


def test_raw_missing_column_rejected() -> None:
    df = _raw_df().drop(columns=["high"])
    with pytest.raises(SCHEMA_ERRORS):
        RawPricesSchema.validate(df, lazy=False)


def test_raw_extra_column_rejected() -> None:
    # strict=True: unexpected columns fail
    df = _raw_df().assign(surprise=1)
    with pytest.raises(SCHEMA_ERRORS):
        RawPricesSchema.validate(df, lazy=False)


def test_raw_lowercase_ticker_rejected() -> None:
    df = _raw_df()
    df["ticker"] = "aapl"
    with pytest.raises(SCHEMA_ERRORS):
        RawPricesSchema.validate(df, lazy=False)


def test_raw_negative_price_rejected() -> None:
    df = _raw_df()
    df.loc[0, "close"] = -1.0
    with pytest.raises(SCHEMA_ERRORS):
        RawPricesSchema.validate(df, lazy=False)


def test_raw_negative_volume_rejected() -> None:
    df = _raw_df()
    df.loc[0, "volume"] = -1
    with pytest.raises(SCHEMA_ERRORS):
        RawPricesSchema.validate(df, lazy=False)


def test_raw_low_greater_than_open_rejected() -> None:
    df = _raw_df()
    df.loc[0, "low"] = 999.0
    with pytest.raises(SCHEMA_ERRORS):
        RawPricesSchema.validate(df, lazy=False)


def test_raw_close_above_high_rejected() -> None:
    df = _raw_df()
    df.loc[0, "close"] = 999.0
    with pytest.raises(SCHEMA_ERRORS):
        RawPricesSchema.validate(df, lazy=False)


# ═══════════════════════════════════════════════════════════
# StgPricesSchema
# ═══════════════════════════════════════════════════════════
def test_stg_valid_frame_passes() -> None:
    StgPricesSchema.validate(_stg_df(), lazy=True)


def test_stg_requires_trade_date_not_date() -> None:
    # _raw_df uses `date`, not `trade_date` — both column names and
    # strictness should reject.
    with pytest.raises(SCHEMA_ERRORS):
        StgPricesSchema.validate(_raw_df(), lazy=False)


def test_stg_ohlc_invariants_enforced() -> None:
    df = _stg_df()
    df.loc[0, "high"] = 50.0  # below open
    with pytest.raises(SCHEMA_ERRORS):
        StgPricesSchema.validate(df, lazy=False)


# ═══════════════════════════════════════════════════════════
# FctReturnsSchema
# ═══════════════════════════════════════════════════════════
def test_marts_valid_frame_passes() -> None:
    FctReturnsSchema.validate(_marts_df(), lazy=True)


def test_marts_null_log_return_allowed_at_boundary() -> None:
    # Row 0 of the fixture has log_return = None by design.
    FctReturnsSchema.validate(_marts_df(), lazy=True)


def test_marts_log_return_out_of_range_rejected() -> None:
    df = _marts_df()
    df.loc[1, "log_return"] = 1.5  # |r| >= 1
    with pytest.raises(SCHEMA_ERRORS):
        FctReturnsSchema.validate(df, lazy=False)


def test_marts_label_null_equivalence_enforced() -> None:
    df = _marts_df()
    # next_log_return not null but label is null
    df.loc[1, "next_return_positive"] = None
    with pytest.raises(SCHEMA_ERRORS):
        FctReturnsSchema.validate(df, lazy=False)


def test_marts_forward_date_before_trade_date_rejected() -> None:
    df = _marts_df()
    # next_trade_date < trade_date
    df["next_trade_date"] = pd.to_datetime([date(2023, 12, 31)] * len(df))
    with pytest.raises(SCHEMA_ERRORS):
        FctReturnsSchema.validate(df, lazy=False)


def test_marts_empty_sector_rejected() -> None:
    df = _marts_df()
    df.loc[0, "sector"] = ""
    with pytest.raises(SCHEMA_ERRORS):
        FctReturnsSchema.validate(df, lazy=False)


# ═══════════════════════════════════════════════════════════
# Macro schemas
# ═══════════════════════════════════════════════════════════
def _macro_df(n: int = 3) -> pd.DataFrame:
    """Well-formed stg_macro_series frame."""
    return pd.DataFrame(
        {
            "series_id": ["FEDFUNDS"] * n,
            "observation_date": pd.to_datetime([date(2024, 1, 1 + i) for i in range(n)]),
            "vintage_date": pd.to_datetime([date(2024, 2, 1 + i) for i in range(n)]),
            "value": [5.33, 5.33, 5.34][:n],
        }
    )


def test_stg_macro_valid_passes() -> None:
    StgMacroSeriesSchema.validate(_macro_df(), lazy=False)


def test_stg_macro_allows_null_value() -> None:
    df = _macro_df()
    df.loc[0, "value"] = None
    StgMacroSeriesSchema.validate(df, lazy=False)


def test_stg_macro_rejects_short_series_id() -> None:
    df = _macro_df()
    df.loc[0, "series_id"] = ""
    with pytest.raises(SCHEMA_ERRORS):
        StgMacroSeriesSchema.validate(df, lazy=False)


def _int_macro_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime([date(2024, 3, 1 + i) for i in range(n)]),
            "series_id": ["FEDFUNDS"] * n,
            "vintage_date": pd.to_datetime([date(2024, 2, 15 + i) for i in range(n)]),
            "observation_date": pd.to_datetime([date(2024, 2, 1)] * n),
            "value": [5.33] * n,
        }
    )


def test_int_macro_valid_passes() -> None:
    IntMacroDailySchema.validate(_int_macro_df(), lazy=False)


def test_int_macro_rejects_vintage_in_future() -> None:
    df = _int_macro_df()
    df.loc[0, "vintage_date"] = df.loc[0, "trade_date"] + pd.Timedelta(days=1)
    with pytest.raises(SCHEMA_ERRORS):
        IntMacroDailySchema.validate(df, lazy=False)


def test_int_macro_rejects_observation_in_future() -> None:
    df = _int_macro_df()
    df.loc[0, "observation_date"] = df.loc[0, "trade_date"] + pd.Timedelta(days=1)
    with pytest.raises(SCHEMA_ERRORS):
        IntMacroDailySchema.validate(df, lazy=False)


# ═══════════════════════════════════════════════════════════
# SEC staging + intermediate
# ═══════════════════════════════════════════════════════════
def _stg_sec_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["AAPL"] * n,
            "cik": [320193] * n,
            "namespace": ["us-gaap"] * n,
            "tag": ["Revenues"] * n,
            "unit": ["USD"] * n,
            "period_start": pd.to_datetime([date(2024, 1, 1)] * n),
            "period_end": [pd.Timestamp("2024-03-31") + pd.Timedelta(days=j) for j in range(n)],
            "filed": pd.to_datetime([date(2024, 5, 3)] * n),
            "form": ["10-Q"] * n,
            "fiscal_year": [2024] * n,
            "fiscal_period": ["Q2"] * n,
            "frame": ["CY2024Q1"] * n,
            "value": [1.0, 2.0, 3.0][:n],
        }
    )


def test_stg_sec_valid_passes() -> None:
    StgSecFactsSchema.validate(_stg_sec_df(), lazy=False)


def test_stg_sec_rejects_lowercase_ticker() -> None:
    df = _stg_sec_df()
    df.loc[0, "ticker"] = "aapl"
    with pytest.raises(SCHEMA_ERRORS):
        StgSecFactsSchema.validate(df, lazy=False)


def test_stg_sec_rejects_period_start_after_end() -> None:
    df = _stg_sec_df()
    df.loc[0, "period_start"] = df.loc[0, "period_end"] + pd.Timedelta(days=1)
    with pytest.raises(SCHEMA_ERRORS):
        StgSecFactsSchema.validate(df, lazy=False)


def _int_fund_df(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime([date(2024, 6, 1 + i) for i in range(n)]),
            "ticker": ["AAPL"] * n,
            "namespace": ["us-gaap"] * n,
            "tag": ["Revenues"] * n,
            "value": [1.0, 2.0, 3.0][:n],
            "filed_used": pd.to_datetime([date(2024, 5, 3)] * n),
            "period_end_used": pd.to_datetime([date(2024, 3, 31)] * n),
        }
    )


def test_int_fund_valid_passes() -> None:
    IntFundamentalsPitSchema.validate(_int_fund_df(), lazy=False)


def test_int_fund_allows_null_when_no_filing() -> None:
    df = _int_fund_df()
    df.loc[0, "value"] = None
    df.loc[0, "filed_used"] = None
    df.loc[0, "period_end_used"] = None
    IntFundamentalsPitSchema.validate(df, lazy=False)


def test_int_fund_rejects_filing_in_future() -> None:
    df = _int_fund_df()
    df.loc[0, "filed_used"] = df.loc[0, "trade_date"] + pd.Timedelta(days=1)
    with pytest.raises(SCHEMA_ERRORS):
        IntFundamentalsPitSchema.validate(df, lazy=False)


def test_int_fund_rejects_period_after_filing() -> None:
    df = _int_fund_df()
    df.loc[0, "period_end_used"] = df.loc[0, "filed_used"] + pd.Timedelta(days=1)
    with pytest.raises(SCHEMA_ERRORS):
        IntFundamentalsPitSchema.validate(df, lazy=False)


def test_int_fund_rejects_period_in_future() -> None:
    df = _int_fund_df()
    df.loc[0, "period_end_used"] = df.loc[0, "trade_date"] + pd.Timedelta(days=1)
    with pytest.raises(SCHEMA_ERRORS):
        IntFundamentalsPitSchema.validate(df, lazy=False)


# ═══════════════════════════════════════════════════════════
# Negative tests on SecFactsSchema (existing)
# ═══════════════════════════════════════════════════════════
def test_sec_schema_allows_filed_before_period_end() -> None:
    """Regression: preliminary 8-K disclosures may predate period_end.

    See ADR 0011 implementation notes: the raw layer mirrors what SEC
    returned; PIT enforcement lives in intermediate.
    """
    df = _stg_sec_df()
    df.loc[0, "filed"] = df.loc[0, "period_end"] - pd.Timedelta(days=10)
    # Must NOT raise: this is legitimate SEC data.
    SecFactsSchema.validate(df, lazy=False)
