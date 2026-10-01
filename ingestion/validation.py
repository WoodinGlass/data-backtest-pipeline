"""Vectorized OHLCV validation.

The pipeline operates on DataFrames, not single rows. Running a Pydantic
model per row (see :class:`~ingestion.schemas.RawPriceEvent`) is 50-100x
slower than pandas vectorized checks for the same invariants. We use both:

- **Pydantic** (:class:`RawPriceEvent`): defensive, single-row contract.
  Suitable for API boundaries, manual corrections, and tests.
- **Vectorized** (:func:`validate_ohlcv_frame`): batch checks in the hot
  path. Same invariants, no per-row object allocation.

Failure is always a hard fail. Bad data must not reach the raw layer.
"""

from __future__ import annotations

import pandas as pd

__all__ = ["OHLCV_REQUIRED", "DataQualityError", "validate_ohlcv_frame"]


# Columns the pipeline requires. `adj_close` must be present (even if
# derived) so downstream code can rely on a stable schema.
OHLCV_REQUIRED: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
)

_PRICE_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "adj_close")


class DataQualityError(ValueError):
    """Raised when a DataFrame fails OHLCV validation.

    Subclass of :class:`ValueError` so callers that already catch
    ``ValueError`` for bad input continue to work unchanged.
    """


def validate_ohlcv_frame(df: pd.DataFrame, *, ticker: str) -> None:
    """Validate that ``df`` is a well-formed OHLCV DataFrame.

    Checks are vectorized and fail fast on the first violation. Raises
    :class:`DataQualityError` with a message that names the ticker and
    up to three offending dates, so the failure is actionable.

    Args:
        df: DataFrame indexed by ``date`` with OHLCV columns.
        ticker: Symbol, used in error messages.

    Raises:
        DataQualityError: on empty input, missing columns, bad index,
            non-positive prices, OHLC invariant violation, negative
            volume, or NaN values.
    """
    if df is None or df.empty:
        raise DataQualityError(f"Refusing to write empty snapshot for {ticker}")

    # ── Schema ──────────────────────────────────────────────
    missing = set(OHLCV_REQUIRED) - set(df.columns)
    if missing:
        raise DataQualityError(f"{ticker}: missing required columns {sorted(missing)}")

    # ── Index ───────────────────────────────────────────────
    if df.index.name != "date":
        raise DataQualityError(f"{ticker}: index name must be 'date', got {df.index.name!r}")
    # Duplicates are checked before monotonicity: a duplicated date is a
    # more specific error than "not sorted", and callers benefit from the
    # precise diagnosis.
    if df.index.has_duplicates:
        raise DataQualityError(f"{ticker}: index has duplicate dates")
    if not df.index.is_monotonic_increasing:
        raise DataQualityError(f"{ticker}: index is not sorted ascending")

    # ── NaN (in any required column) ────────────────────────
    nan_mask = df[list(OHLCV_REQUIRED)].isna()
    if nan_mask.any().any():
        bad_cols = nan_mask.any()[nan_mask.any()].index.tolist()
        raise DataQualityError(f"{ticker}: NaN values present in columns {bad_cols}")

    # ── Strictly positive prices ────────────────────────────
    for col in _PRICE_COLUMNS:
        mask = df[col] <= 0
        if mask.any():
            bad_dates = [str(d) for d in df.index[mask][:3]]
            raise DataQualityError(f"{ticker}: non-positive {col} at {bad_dates}")

    # ── OHLC invariants ─────────────────────────────────────
    # low <= open <= high, low <= close <= high
    checks = (
        ("low", "open", "low > open", df["low"] > df["open"]),
        ("low", "close", "low > close", df["low"] > df["close"]),
        ("open", "high", "open > high", df["open"] > df["high"]),
        ("close", "high", "close > high", df["close"] > df["high"]),
    )
    for _lhs, _rhs, label, mask in checks:
        if mask.any():
            bad_dates = [str(d) for d in df.index[mask][:3]]
            raise DataQualityError(f"{ticker}: {label} at {bad_dates}")

    # ── Volume non-negative ─────────────────────────────────
    vol_mask = df["volume"] < 0
    if vol_mask.any():
        bad_dates = [str(d) for d in df.index[vol_mask][:3]]
        raise DataQualityError(f"{ticker}: negative volume at {bad_dates}")
