"""Price data client.

Wraps a data provider (default: yfinance) behind a narrow interface
so the rest of the pipeline does not depend on any specific vendor.

Design:
- ``PriceSource`` Protocol: the interface. Any provider that returns a
  normalized OHLCV DataFrame satisfies it.
- ``YFinanceClient``: the default implementation.
- Retry with exponential backoff + jitter on transient failures.
- Rate limiting between calls, so we do not get throttled upstream.
- Timezone-aware datetimes are normalized to naive UTC dates.
- Empty results raise ``PriceFetchError``; we never silently return
  an empty DataFrame.

Usage:
    from ingestion.client import YFinanceClient
    from datetime import date

    client = YFinanceClient()
    df = client.fetch("AAPL", start=date(2024, 1, 1), end=date(2024, 3, 1))
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

import pandas as pd
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ingestion.config import Settings, get_settings
from ingestion.logging import get_logger

__all__ = [
    "PriceFetchError",
    "PriceSource",
    "YFinanceClient",
    "normalize_ohlcv",
]


log = get_logger(__name__)


# Canonical column names produced by every PriceSource.
OHLCV_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
)


class PriceFetchError(RuntimeError):
    """Raised when a price fetch fails after all retries are exhausted."""


class PriceSource(Protocol):
    """Interface implemented by every price data provider."""

    def fetch(self, ticker: str, *, start: date, end: date) -> pd.DataFrame:
        """Return a normalized OHLCV DataFrame for ``ticker``.

        Args:
            ticker: Symbol, e.g. ``"AAPL"``.
            start: Inclusive start date.
            end: Exclusive end date (matches yfinance convention).

        Returns:
            DataFrame indexed by ``date`` (naive, normalized to UTC date),
            with columns exactly ``OHLCV_COLUMNS``.

        Raises:
            PriceFetchError: if the fetch fails or returns no data.
        """
        ...


# ────────────────────────────────────────────────────────────
# Normalization
# ────────────────────────────────────────────────────────────
def normalize_ohlcv(df: pd.DataFrame, *, ticker: str) -> pd.DataFrame:
    """Normalize a raw yfinance-style DataFrame to canonical form.

    Guarantees:
    - Index is a ``date`` (not datetime), naive, sorted ascending, unique.
    - Columns are exactly :data:`OHLCV_COLUMNS`.
    - ``adj_close`` falls back to ``close`` if the source omits it.
    - ``volume`` is coerced to ``int64``.
    - OHLC values are ``float64``.

    Args:
        df: Raw DataFrame from the provider.
        ticker: Symbol, used for logging and error messages.

    Returns:
        Normalized DataFrame.

    Raises:
        PriceFetchError: if ``df`` is empty or missing required columns.
    """
    if df is None or df.empty:
        raise PriceFetchError(f"No data returned for {ticker}")

    # Drop yfinance's extra columns (Dividends, Stock Splits) if present.
    # Be tolerant of both 'Adj Close' (older) and 'adj_close' (already normalized).
    rename_map = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",
        "adj_close": "adj_close",
        "Volume": "volume",
        "volume": "volume",
    }
    df = df.rename(columns=rename_map)

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise PriceFetchError(f"Missing required columns for {ticker}: {sorted(missing)}")

    # adj_close fallback
    if "adj_close" not in df.columns:
        df = df.assign(adj_close=df["close"])

    # Ensure UTC date index (naive)
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    df.index = idx.normalize()
    df.index.name = "date"

    # Keep canonical columns, coerce dtypes
    df = df.loc[:, list(OHLCV_COLUMNS)].copy()
    for col in ("open", "high", "low", "close", "adj_close"):
        df[col] = df[col].astype("float64")
    df["volume"] = df["volume"].fillna(0).astype("int64")

    # Drop rows with any NaN in OHLC (defensive; yfinance sometimes emits them)
    df = df.dropna(subset=["open", "high", "low", "close", "adj_close"])
    if df.empty:
        raise PriceFetchError(f"All rows for {ticker} had NaN OHLC values")

    # Sort + dedupe index
    df = df[~df.index.duplicated(keep="last")].sort_index()

    # Store date (not Timestamp) as index.
    # Cast to DatetimeIndex explicitly so mypy knows `.date` is available
    # and so the result type is well-defined across pandas-stubs versions.
    df.index = pd.Index(pd.DatetimeIndex(df.index).date, name="date")
    return df


# ────────────────────────────────────────────────────────────
# Client
# ────────────────────────────────────────────────────────────
@dataclass
class YFinanceClient:
    """PriceSource backed by yfinance.

    Args:
        rate_limit_seconds: Minimum gap between successive network calls.
            Set to 0 to disable (useful for tests).
        max_retries: Attempts before giving up.
        retry_min_seconds / retry_max_seconds: Backoff bounds.
        sleep: Injectable sleep, for tests. Defaults to ``time.sleep``.
        settings: Optional pre-built Settings; defaults to ``get_settings()``.
    """

    rate_limit_seconds: float | None = None
    max_retries: int | None = None
    retry_min_seconds: float | None = None
    retry_max_seconds: float | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)
    settings: Settings = field(default_factory=get_settings)

    _last_call_ts: float = field(default=0.0, init=False, repr=False)

    # ── Public API ──────────────────────────────────────────
    def fetch(self, ticker: str, *, start: date, end: date) -> pd.DataFrame:
        """Fetch and normalize OHLCV data for ``ticker``.

        Retries transient failures with exponential backoff + jitter.
        Enforces a minimum gap between network calls.

        Raises:
            PriceFetchError: on empty results or exhausted retries.
        """
        self._rate_limit()
        return self._fetch_with_retry(ticker.upper(), start=start, end=end)

    # ── Internal ────────────────────────────────────────────
    def _rate_limit(self) -> None:
        limit = (
            self.rate_limit_seconds
            if self.rate_limit_seconds is not None
            else self.settings.ingest_rate_limit_seconds
        )
        if limit <= 0:
            self._last_call_ts = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_call_ts
        wait = limit - elapsed
        if self._last_call_ts > 0 and wait > 0:
            self.sleep(wait)
        self._last_call_ts = time.monotonic()

    def _fetch_with_retry(self, ticker: str, *, start: date, end: date) -> pd.DataFrame:
        max_retries = (
            self.max_retries if self.max_retries is not None else self.settings.ingest_max_retries
        )
        retry_min = (
            self.retry_min_seconds
            if self.retry_min_seconds is not None
            else self.settings.ingest_retry_min_seconds
        )
        retry_max = (
            self.retry_max_seconds
            if self.retry_max_seconds is not None
            else self.settings.ingest_retry_max_seconds
        )

        @retry(
            stop=stop_after_attempt(max_retries),
            wait=wait_exponential_jitter(initial=retry_min, max=retry_max),
            retry=retry_if_exception_type((PriceFetchError, ConnectionError, TimeoutError)),
            before_sleep=self._log_retry(ticker),
            reraise=True,
        )
        def _do() -> pd.DataFrame:
            return self._fetch_once(ticker, start=start, end=end)

        return _do()

    def _log_retry(self, ticker: str) -> Callable[[RetryCallState], None]:
        def _hook(state: RetryCallState) -> None:
            outcome = state.outcome
            exc = outcome.exception() if outcome is not None else None
            log.warning(
                "fetch_retry",
                ticker=ticker,
                attempt=state.attempt_number,
                error=repr(exc) if exc is not None else None,
            )

        return _hook

    def _fetch_once(self, ticker: str, *, start: date, end: date) -> pd.DataFrame:
        """Single attempt against yfinance. Raises on any failure."""
        # Imported lazily so that tests that monkeypatch yf still work,
        # and so importing this module does not require network access.
        import yfinance as yf

        log.info("fetch_start", ticker=ticker, start=str(start), end=str(end))
        try:
            raw = yf.Ticker(ticker).history(
                start=start.isoformat(),
                end=end.isoformat(),
                auto_adjust=False,
                actions=False,
                timeout=15,
            )
        except Exception as exc:
            raise PriceFetchError(f"yfinance raised for {ticker}: {exc!r}") from exc

        df = normalize_ohlcv(raw, ticker=ticker)
        log.info(
            "fetch_ok",
            ticker=ticker,
            rows=len(df),
            first=str(df.index[0]) if len(df) else None,
            last=str(df.index[-1]) if len(df) else None,
        )
        return df
