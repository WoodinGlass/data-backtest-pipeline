"""Unit tests for quality/schemas.py — no external services."""

from datetime import date

import pandas as pd
import pandera.pandas as pa
import pytest

from quality.schemas import (
    SCHEMAS,
    FctReturnsSchema,
    RawPricesSchema,
    StgPricesSchema,
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
def test_schemas_registry_has_three_layers() -> None:
    assert set(SCHEMAS.keys()) == {"raw", "staging", "marts"}


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
