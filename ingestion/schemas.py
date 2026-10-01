"""Data contracts for the ingestion layer.

This module defines the schema for records written to the raw layer.
Contracts are defined here *before* any producer or consumer code,
so that both sides cannot drift.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["RawPriceEvent", "payload_hash", "utcnow"]


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(tz=UTC)


def payload_hash(payload: dict[str, Any]) -> str:
    """Return a stable SHA-256 hash of a JSON-serialisable payload.

    Keys are sorted so that logically-equal payloads hash identically.
    This is what makes idempotent writes possible: two ingestion runs
    that observe the same OHLCV bar produce the same hash, so the
    second write is a no-op.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RawPriceEvent(BaseModel):
    """One immutable daily OHLCV bar for a single ticker.

    Natural key: ``(source, ticker, trade_date)``.
    ``payload_hash`` captures the exact OHLCV values, so an upstream
    revision (split, dividend, restatement) is stored as a *new* row,
    never as an update. The raw layer is append-only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    ticker: str = Field(min_length=1, max_length=16)
    trade_date: date
    ingested_at: datetime = Field(default_factory=utcnow)
    payload_hash: str = Field(min_length=64, max_length=64)

    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    adj_close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)

    @property
    def idempotency_key(self) -> str:
        """Composite key used to dedupe writes to the raw layer."""
        return f"{self.source}:{self.ticker}:{self.trade_date.isoformat()}:{self.payload_hash}"

    @model_validator(mode="after")
    def _check_ohlc_consistency(self) -> RawPriceEvent:
        """Enforce the OHLC invariant: ``low <= open, close <= high``.

        Violations here mean corrupt data, so we fail hard rather than
        letting it silently propagate to features and backtests.
        """
        if not (self.low <= self.open <= self.high):
            raise ValueError(
                f"OHLC invariant violated (low<=open<=high): "
                f"low={self.low}, open={self.open}, high={self.high}"
            )
        if not (self.low <= self.close <= self.high):
            raise ValueError(
                f"OHLC invariant violated (low<=close<=high): "
                f"low={self.low}, close={self.close}, high={self.high}"
            )
        return self

    @staticmethod
    def ohlcv_payload(
        source: str,
        ticker: str,
        trade_date: date,
        open_: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        adj_close: Decimal,
        volume: int,
    ) -> dict[str, Any]:
        """Canonical payload used as input to :func:`payload_hash`."""
        return {
            "source": source,
            "ticker": ticker,
            "trade_date": trade_date.isoformat(),
            "open": str(open_),
            "high": str(high),
            "low": str(low),
            "close": str(close),
            "adj_close": str(adj_close),
            "volume": volume,
        }

    @classmethod
    def from_ohlcv(
        cls,
        source: str,
        ticker: str,
        trade_date: date,
        open_: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        adj_close: Decimal,
        volume: int,
    ) -> RawPriceEvent:
        """Build a :class:`RawPriceEvent` with an auto-computed ``payload_hash``."""
        payload = cls.ohlcv_payload(
            source, ticker, trade_date, open_, high, low, close, adj_close, volume
        )
        return cls(
            source=source,
            ticker=ticker,
            trade_date=trade_date,
            payload_hash=payload_hash(payload),
            open=open_,
            high=high,
            low=low,
            close=close,
            adj_close=adj_close,
            volume=volume,
        )
