"""Unit tests for ingestion/sec/schemas.py."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from ingestion.sec.schemas import (
    SecCompanyFacts,
    SecFact,
    sec_facts_hash,
)


def _fact(**overrides: object) -> SecFact:
    defaults: dict[str, object] = {
        "ticker": "AAPL",
        "cik": 320193,
        "namespace": "us-gaap",
        "tag": "Revenues",
        "unit": "USD",
        "period_start": date(2024, 1, 1),
        "period_end": date(2024, 3, 31),
        "filed": date(2024, 5, 3),
        "form": "10-Q",
        "fiscal_year": 2024,
        "fiscal_period": "Q2",
        "frame": "CY2024Q1",
        "value": 90_753_000_000.0,
    }
    defaults.update(overrides)
    return SecFact(**defaults)  # type: ignore[arg-type]


def _snapshot(facts: list[SecFact], **overrides: object) -> SecCompanyFacts:
    defaults: dict[str, object] = {
        "ticker": "AAPL",
        "cik": 320193,
        "entity_name": "Apple Inc.",
        "facts": facts,
    }
    defaults.update(overrides)
    return SecCompanyFacts(**defaults)  # type: ignore[arg-type]


# ─── SecFact ───────────────────────────────────────────────
def test_fact_construction() -> None:
    f = _fact()
    assert f.ticker == "AAPL"
    assert f.tag == "Revenues"
    assert f.value == pytest.approx(90_753_000_000.0)


def test_fact_is_frozen() -> None:
    f = _fact()
    with pytest.raises(ValidationError):
        f.value = 0.0  # type: ignore[misc]


def test_fact_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        SecFact(  # type: ignore[call-arg]
            ticker="AAPL",
            cik=1,
            namespace="us-gaap",
            tag="X",
            unit="USD",
            period_end=date(2024, 1, 1),
            filed=date(2024, 1, 1),
            form="10-Q",
            value=1.0,
            extra="nope",
        )


def test_fact_allows_none_value() -> None:
    f = _fact(value=None)
    assert f.value is None


def test_fact_accepts_point_in_time_no_start() -> None:
    f = _fact(period_start=None)
    assert f.period_start is None


def test_fact_normalises_empty_fiscal_period() -> None:
    f = _fact(fiscal_period="")
    assert f.fiscal_period is None


def test_fact_normalises_whitespace_fiscal_period() -> None:
    f = _fact(fiscal_period="   ")
    assert f.fiscal_period is None


# ─── SecCompanyFacts ───────────────────────────────────────
def test_snapshot_counts() -> None:
    facts = [
        _fact(tag="A", value=1.0),
        _fact(tag="B", value=None),
        _fact(tag="C", value=3.0),
    ]
    s = _snapshot(facts)
    assert s.n_facts == 3
    assert s.n_non_null == 2
    assert s.n_tags == 3


def test_snapshot_date_bounds() -> None:
    facts = [
        _fact(filed=date(2020, 1, 1)),
        _fact(filed=date(2024, 5, 3)),
        _fact(filed=date(2022, 6, 15)),
    ]
    s = _snapshot(facts)
    assert s.first_filed == date(2020, 1, 1)
    assert s.last_filed == date(2024, 5, 3)


def test_snapshot_empty_bounds_none() -> None:
    s = _snapshot([])
    assert s.first_filed is None
    assert s.last_filed is None
    assert s.n_facts == 0


# ─── sec_facts_hash ────────────────────────────────────────
def test_hash_deterministic() -> None:
    s = _snapshot([_fact(), _fact(tag="CostOfRevenue", value=50_000.0)])
    h1 = sec_facts_hash(s)
    h2 = sec_facts_hash(s)
    assert h1 == h2
    assert len(h1) == 64


def test_hash_insensitive_to_order() -> None:
    f1 = _fact(tag="A", value=1.0)
    f2 = _fact(tag="B", value=2.0)
    s_a = _snapshot([f1, f2])
    s_b = _snapshot([f2, f1])
    assert sec_facts_hash(s_a) == sec_facts_hash(s_b)


def test_hash_sensitive_to_value() -> None:
    s_a = _snapshot([_fact(value=1.0)])
    s_b = _snapshot([_fact(value=1.1)])
    assert sec_facts_hash(s_a) != sec_facts_hash(s_b)


def test_hash_sensitive_to_cik() -> None:
    s_a = _snapshot([_fact()], cik=320193)
    s_b = _snapshot([_fact()], cik=1)
    assert sec_facts_hash(s_a) != sec_facts_hash(s_b)


def test_hash_sensitive_to_entity_name() -> None:
    s_a = _snapshot([_fact()], entity_name="Apple Inc.")
    s_b = _snapshot([_fact()], entity_name="Apple")
    assert sec_facts_hash(s_a) != sec_facts_hash(s_b)


def test_hash_insensitive_to_fetched_at() -> None:
    facts = [_fact()]
    s_a = _snapshot(facts, fetched_at=datetime(2024, 1, 1, tzinfo=UTC))
    s_b = _snapshot(facts, fetched_at=datetime(2026, 6, 1, tzinfo=UTC))
    assert sec_facts_hash(s_a) == sec_facts_hash(s_b)


def test_hash_handles_null_value() -> None:
    s_a = _snapshot([_fact(value=None)])
    s_b = _snapshot([_fact(value=None)])
    assert sec_facts_hash(s_a) == sec_facts_hash(s_b)
