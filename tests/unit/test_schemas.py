"""Unit tests for ingestion/schemas.py — pure, no external services."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ingestion.schemas import RawPriceEvent, payload_hash, utcnow

# ─── payload_hash ───────────────────────────────────────────


def test_payload_hash_is_stable_across_key_order() -> None:
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert payload_hash(a) == payload_hash(b)


def test_payload_hash_changes_when_payload_changes() -> None:
    assert payload_hash({"a": 1}) != payload_hash({"a": 2})


def test_payload_hash_is_hex_sha256() -> None:
    h = payload_hash({"x": 1})
    assert len(h) == 64
    int(h, 16)  # must be valid hex


# ─── RawPriceEvent construction ─────────────────────────────


def _make(**overrides: object) -> RawPriceEvent:
    defaults: dict[str, object] = {
        "source": "yfinance",
        "ticker": "AAPL",
        "trade_date": date(2024, 1, 2),
        "open_": Decimal("185.00"),
        "high": Decimal("187.00"),
        "low": Decimal("184.50"),
        "close": Decimal("186.50"),
        "adj_close": Decimal("186.50"),
        "volume": 45_000_000,
    }
    defaults.update(overrides)
    return RawPriceEvent.from_ohlcv(**defaults)  # type: ignore[arg-type]


def test_from_ohlcv_populates_payload_hash() -> None:
    event = _make()
    assert len(event.payload_hash) == 64
    int(event.payload_hash, 16)


def test_from_ohlcv_hash_is_deterministic() -> None:
    assert _make().payload_hash == _make().payload_hash


def test_from_ohlcv_hash_changes_when_price_changes() -> None:
    a = _make(close=Decimal("186.50"))
    b = _make(close=Decimal("186.51"))
    assert a.payload_hash != b.payload_hash


# ─── idempotency_key ───────────────────────────────────────


def test_idempotency_key_is_composite() -> None:
    event = _make()
    assert event.idempotency_key.startswith("yfinance:AAPL:2024-01-02:")
    assert event.idempotency_key.endswith(event.payload_hash)


# ─── immutability & validation ─────────────────────────────


def test_raw_price_event_is_frozen() -> None:
    event = _make()
    with pytest.raises(ValidationError):
        event.ticker = "MSFT"  # type: ignore[misc]


def test_rejects_missing_required_fields() -> None:
    with pytest.raises(ValidationError):
        RawPriceEvent(ticker="AAPL")  # type: ignore[call-arg]


def test_rejects_bad_hash_length() -> None:
    with pytest.raises(ValidationError):
        RawPriceEvent(
            source="yfinance",
            ticker="AAPL",
            trade_date=date(2024, 1, 2),
            payload_hash="too-short",
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            adj_close=Decimal("1"),
            volume=0,
        )


def test_rejects_negative_volume() -> None:
    with pytest.raises(ValidationError):
        _make(volume=-1)


def test_rejects_non_positive_prices() -> None:
    with pytest.raises(ValidationError):
        _make(close=Decimal("0"))


def test_rejects_high_below_open() -> None:
    with pytest.raises(ValidationError):
        _make(
            open_=Decimal("200"),
            high=Decimal("100"),
            low=Decimal("50"),
            close=Decimal("150"),
        )


def test_rejects_low_above_close() -> None:
    with pytest.raises(ValidationError):
        _make(
            open_=Decimal("100"),
            high=Decimal("300"),
            low=Decimal("200"),
            close=Decimal("150"),
        )


def test_utcnow_is_timezone_aware() -> None:
    now = utcnow()
    assert isinstance(now, datetime)
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(None)
