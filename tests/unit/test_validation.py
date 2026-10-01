"""Unit tests for ingestion/validation.py — vectorized OHLCV checks."""

from datetime import date

import pandas as pd
import pytest

from ingestion.validation import DataQualityError, validate_ohlcv_frame


def _df(n: int = 3, **overrides: list[float] | list[int]) -> pd.DataFrame:
    idx = pd.Index([date(2024, 1, 2 + i) for i in range(n)], name="date")
    data: dict[str, list[float] | list[int]] = {
        "open": [100.0 + i for i in range(n)],
        "high": [102.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [101.0 + i for i in range(n)],
        "adj_close": [101.0 + i for i in range(n)],
        "volume": [1000 * (i + 1) for i in range(n)],
    }
    data.update(overrides)
    return pd.DataFrame(data, index=idx)


# ─── Happy path ─────────────────────────────────────────────
def test_valid_frame_passes() -> None:
    validate_ohlcv_frame(_df(), ticker="TEST")


def test_error_is_valueerror_subclass() -> None:
    with pytest.raises(ValueError):
        validate_ohlcv_frame(pd.DataFrame(), ticker="X")


# ─── Structure ──────────────────────────────────────────────
def test_empty_dataframe_rejected() -> None:
    with pytest.raises(DataQualityError, match="empty snapshot"):
        validate_ohlcv_frame(pd.DataFrame(), ticker="X")


def test_none_rejected() -> None:
    with pytest.raises(DataQualityError, match="empty snapshot"):
        validate_ohlcv_frame(None, ticker="X")  # type: ignore[arg-type]


def test_missing_column_rejected() -> None:
    df = _df().drop(columns=["high"])
    with pytest.raises(DataQualityError, match="missing required columns"):
        validate_ohlcv_frame(df, ticker="X")


def test_index_must_be_named_date() -> None:
    df = _df()
    df.index.name = "foo"
    with pytest.raises(DataQualityError, match="index name must be 'date'"):
        validate_ohlcv_frame(df, ticker="X")


def test_index_must_be_sorted() -> None:
    df = _df().iloc[[2, 0, 1]]
    with pytest.raises(DataQualityError, match="not sorted ascending"):
        validate_ohlcv_frame(df, ticker="X")


def test_index_must_be_unique() -> None:
    df = _df()
    # Insert a duplicate of the first row right after itself so the
    # index stays sorted; the duplicate check must fire regardless.
    dup = df.iloc[[0]]
    df = pd.concat([df.iloc[[0]], dup, df.iloc[1:]])
    df.index.name = "date"
    with pytest.raises(DataQualityError, match="duplicate dates"):
        validate_ohlcv_frame(df, ticker="X")


# ─── Values ─────────────────────────────────────────────────
def test_nan_rejected() -> None:
    df = _df()
    df.iloc[0, df.columns.get_loc("close")] = float("nan")
    with pytest.raises(DataQualityError, match="NaN values"):
        validate_ohlcv_frame(df, ticker="X")


def test_zero_price_rejected() -> None:
    df = _df()
    df.iloc[0, df.columns.get_loc("open")] = 0.0
    with pytest.raises(DataQualityError, match="non-positive open"):
        validate_ohlcv_frame(df, ticker="X")


def test_negative_price_rejected() -> None:
    df = _df()
    df.iloc[0, df.columns.get_loc("adj_close")] = -1.0
    with pytest.raises(DataQualityError, match="non-positive adj_close"):
        validate_ohlcv_frame(df, ticker="X")


def test_low_above_open_rejected() -> None:
    df = _df()
    df.iloc[0, df.columns.get_loc("low")] = 200.0
    with pytest.raises(DataQualityError, match="low > open"):
        validate_ohlcv_frame(df, ticker="X")


def test_open_above_high_rejected() -> None:
    df = _df()
    df.iloc[0, df.columns.get_loc("open")] = 500.0
    with pytest.raises(DataQualityError, match="open > high"):
        validate_ohlcv_frame(df, ticker="X")


def test_close_above_high_rejected() -> None:
    df = _df()
    df.iloc[0, df.columns.get_loc("close")] = 500.0
    with pytest.raises(DataQualityError, match="close > high"):
        validate_ohlcv_frame(df, ticker="X")


def test_negative_volume_rejected() -> None:
    df = _df()
    df.iloc[0, df.columns.get_loc("volume")] = -1
    with pytest.raises(DataQualityError, match="negative volume"):
        validate_ohlcv_frame(df, ticker="X")


def test_zero_volume_allowed() -> None:
    df = _df()
    df.iloc[0, df.columns.get_loc("volume")] = 0
    validate_ohlcv_frame(df, ticker="X")


def test_error_message_names_ticker_and_date() -> None:
    df = _df()
    df.iloc[0, df.columns.get_loc("low")] = 200.0
    with pytest.raises(DataQualityError, match="X: low > open at"):
        validate_ohlcv_frame(df, ticker="X")
