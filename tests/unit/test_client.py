"""Unit tests for ingestion/client.py — no real network calls."""

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from ingestion.client import (
    OHLCV_COLUMNS,
    PriceFetchError,
    YFinanceClient,
    normalize_ohlcv,
)


# ─── helpers ────────────────────────────────────────────────
def _raw_df(
    n: int = 3,
    *,
    include_adj_close: bool = True,
    include_extras: bool = True,
    tz: str | None = None,
) -> pd.DataFrame:
    idx = pd.date_range("2024-01-02", periods=n, freq="B", tz=tz)
    data: dict[str, list[float] | list[int]] = {
        "Open": [100.0 + i for i in range(n)],
        "High": [102.0 + i for i in range(n)],
        "Low": [99.0 + i for i in range(n)],
        "Close": [101.0 + i for i in range(n)],
        "Volume": [1000 * (i + 1) for i in range(n)],
    }
    if include_adj_close:
        data["Adj Close"] = [101.0 + i for i in range(n)]
    if include_extras:
        data["Dividends"] = [0.0] * n
        data["Stock Splits"] = [0.0] * n
    return pd.DataFrame(data, index=idx)


# ─── normalize_ohlcv ────────────────────────────────────────
def test_normalize_returns_canonical_columns() -> None:
    out = normalize_ohlcv(_raw_df(), ticker="TEST")
    assert tuple(out.columns) == OHLCV_COLUMNS


def test_normalize_index_is_date_not_timestamp() -> None:
    out = normalize_ohlcv(_raw_df(), ticker="TEST")
    assert isinstance(out.index[0], date)
    assert not isinstance(out.index[0], pd.Timestamp)
    assert out.index.name == "date"


def test_normalize_is_sorted_and_deduplicated() -> None:
    df = _raw_df(n=3)
    shuffled = df.iloc[[2, 0, 1]]
    out = normalize_ohlcv(shuffled, ticker="TEST")
    assert list(out.index) == sorted(out.index)
    assert len(out.index) == len(set(out.index))


def test_normalize_adj_close_falls_back_to_close() -> None:
    df = _raw_df(include_adj_close=False)
    out = normalize_ohlcv(df, ticker="TEST")
    pd.testing.assert_series_equal(out["adj_close"], out["close"], check_names=False)


def test_normalize_drops_extra_columns() -> None:
    out = normalize_ohlcv(_raw_df(include_extras=True), ticker="TEST")
    assert "Dividends" not in out.columns
    assert "Stock Splits" not in out.columns


def test_normalize_coerces_volume_to_int64() -> None:
    out = normalize_ohlcv(_raw_df(), ticker="TEST")
    assert out["volume"].dtype == "int64"


def test_normalize_coerces_prices_to_float64() -> None:
    out = normalize_ohlcv(_raw_df(), ticker="TEST")
    for col in ("open", "high", "low", "close", "adj_close"):
        assert out[col].dtype == "float64"


def test_normalize_handles_tz_aware_index() -> None:
    out = normalize_ohlcv(_raw_df(tz="America/New_York"), ticker="TEST")
    # After normalization, the index holds plain date objects — which
    # have no timezone concept — so tz information is gone by construction.
    assert all(isinstance(d, date) and not hasattr(d, "tz") for d in out.index)
    # And the normalized date must match the source calendar date in NY.
    assert out.index[0] == date(2024, 1, 2)


def test_normalize_raises_on_empty_df() -> None:
    with pytest.raises(PriceFetchError, match="No data returned"):
        normalize_ohlcv(pd.DataFrame(), ticker="EMPTY")


def test_normalize_raises_on_missing_required_columns() -> None:
    df = _raw_df().drop(columns=["High"])
    with pytest.raises(PriceFetchError, match="Missing required columns"):
        normalize_ohlcv(df, ticker="BROKEN")


def test_normalize_raises_when_all_ohlc_are_nan() -> None:
    df = _raw_df(n=2)
    df[["Open", "High", "Low", "Close", "Adj Close"]] = float("nan")
    with pytest.raises(PriceFetchError, match="NaN OHLC"):
        normalize_ohlcv(df, ticker="NANS")


def test_normalize_drops_rows_with_nan_ohlc() -> None:
    df = _raw_df(n=3)
    df.loc[df.index[1], "Close"] = float("nan")
    out = normalize_ohlcv(df, ticker="PARTIAL")
    assert len(out) == 2


# ─── YFinanceClient — fetch wrapper ─────────────────────────
@pytest.fixture
def fake_yf(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a fake yfinance module whose Ticker().history() is controlled."""
    fake_module = MagicMock()
    fake_ticker = MagicMock()
    fake_module.Ticker.return_value = fake_ticker
    # Place into sys.modules under 'yfinance' so `import yfinance` gets it
    import sys

    monkeypatch.setitem(sys.modules, "yfinance", fake_module)
    return fake_module


def test_fetch_returns_normalized_frame(fake_yf: MagicMock) -> None:
    fake_yf.Ticker.return_value.history.return_value = _raw_df(n=5)
    client = YFinanceClient(rate_limit_seconds=0, max_retries=1)
    out = client.fetch("AAPL", start=date(2024, 1, 1), end=date(2024, 2, 1))
    assert len(out) == 5
    assert tuple(out.columns) == OHLCV_COLUMNS
    fake_yf.Ticker.assert_called_once_with("AAPL")


def test_fetch_uppercases_ticker(fake_yf: MagicMock) -> None:
    fake_yf.Ticker.return_value.history.return_value = _raw_df()
    client = YFinanceClient(rate_limit_seconds=0, max_retries=1)
    client.fetch("aapl", start=date(2024, 1, 1), end=date(2024, 2, 1))
    fake_yf.Ticker.assert_called_once_with("AAPL")


def test_fetch_raises_on_empty_response(fake_yf: MagicMock) -> None:
    fake_yf.Ticker.return_value.history.return_value = pd.DataFrame()
    client = YFinanceClient(rate_limit_seconds=0, max_retries=1)
    with pytest.raises(PriceFetchError, match="No data returned"):
        client.fetch("MISSING", start=date(2024, 1, 1), end=date(2024, 2, 1))


def test_fetch_wraps_provider_exception(fake_yf: MagicMock) -> None:
    fake_yf.Ticker.return_value.history.side_effect = RuntimeError("boom")
    client = YFinanceClient(rate_limit_seconds=0, max_retries=1)
    with pytest.raises(PriceFetchError, match="yfinance raised"):
        client.fetch("AAPL", start=date(2024, 1, 1), end=date(2024, 2, 1))


def test_fetch_retries_then_succeeds(fake_yf: MagicMock) -> None:
    hist = fake_yf.Ticker.return_value.history
    hist.side_effect = [
        PriceFetchError("transient"),
        PriceFetchError("transient"),
        _raw_df(n=3),
    ]
    client = YFinanceClient(
        rate_limit_seconds=0,
        max_retries=5,
        retry_min_seconds=0.001,
        retry_max_seconds=0.01,
    )
    out = client.fetch("AAPL", start=date(2024, 1, 1), end=date(2024, 2, 1))
    assert len(out) == 3
    assert hist.call_count == 3


def test_fetch_gives_up_after_max_retries(fake_yf: MagicMock) -> None:
    fake_yf.Ticker.return_value.history.side_effect = PriceFetchError("always")
    client = YFinanceClient(
        rate_limit_seconds=0,
        max_retries=3,
        retry_min_seconds=0.001,
        retry_max_seconds=0.01,
    )
    with pytest.raises(PriceFetchError):
        client.fetch("AAPL", start=date(2024, 1, 1), end=date(2024, 2, 1))
    assert fake_yf.Ticker.return_value.history.call_count == 3


def test_rate_limit_sleeps_between_calls(fake_yf: MagicMock) -> None:
    fake_yf.Ticker.return_value.history.return_value = _raw_df()
    slept: list[float] = []
    client = YFinanceClient(
        rate_limit_seconds=0.2,
        max_retries=1,
        sleep=lambda s: slept.append(s),
    )
    client.fetch("A", start=date(2024, 1, 1), end=date(2024, 2, 1))
    client.fetch("B", start=date(2024, 1, 1), end=date(2024, 2, 1))
    assert len(slept) == 1
    assert 0 < slept[0] <= 0.2


def test_rate_limit_disabled_when_zero(fake_yf: MagicMock) -> None:
    fake_yf.Ticker.return_value.history.return_value = _raw_df()
    slept: list[float] = []
    client = YFinanceClient(rate_limit_seconds=0, max_retries=1, sleep=slept.append)
    client.fetch("A", start=date(2024, 1, 1), end=date(2024, 2, 1))
    client.fetch("B", start=date(2024, 1, 1), end=date(2024, 2, 1))
    assert slept == []
