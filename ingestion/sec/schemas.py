"""Pydantic contracts for SEC EDGAR fundamental ingestion.

One fact = one (ticker, tag, namespace, period_end, filed, form) tuple
with a numeric value. A snapshot = all facts for one ticker at one
point in time.

Design notes
------------
- SEC EDGAR's companyfacts response has this shape:
    {"cik": ..., "entityName": "...", "facts": {
        "us-gaap": {"Revenues": {"units": {"USD": [
            {"start": ..., "end": ..., "val": ..., "fy": ..., "fp": ...,
             "form": "10-K", "filed": "2024-02-15", "frame": "CY2023"},
            ...
        ]}}},
        "dei": {...}
    }}

  A single (tag, unit) can appear many times (multiple filings). Each
  row has its own `filed` date — the PIT key.

- `value` is `float | None` because EDGAR occasionally returns null
  for specific periods (e.g. a segment discontinued mid-year).

- `filed` is the PIT key: the date the fact first appeared in a filing.
  Downstream joins use `filed <= trade_date`.

- Content hash for idempotency covers (cik, entity_name, facts) with
  values rounded to 4 decimal places. `fetched_at` is excluded so a
  refetch of the same content produces the same hash (see ADR 0006).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "SecCompanyFacts",
    "SecFact",
    "sec_facts_hash",
    "utcnow",
]

# SEC returns some numeric values with many decimals; rounding to this
# precision keeps hashes stable against vendor noise.
HASH_DECIMALS = 4


def utcnow() -> datetime:
    """Current UTC time as a timezone-aware datetime."""
    return datetime.now(tz=UTC)


class SecFact(BaseModel):
    """One XBRL fact from SEC EDGAR companyfacts.

    Natural key: (ticker, namespace, tag, period_end, filed, form,
    frame). `frame` disambiguates facts from overlapping filings when
    the same period appears in both a 10-Q and a 10-K.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(min_length=1, max_length=16)
    cik: int = Field(ge=0)
    namespace: str = Field(min_length=1, max_length=16)  # us-gaap | dei | ifrs-full
    tag: str = Field(min_length=1, max_length=256)
    unit: str = Field(min_length=1, max_length=32)  # USD | shares | pure | ...

    # Period the fact describes.
    period_start: date | None = None  # NULL for point-in-time facts
    period_end: date

    # Filing metadata (PIT).
    filed: date  # when the fact first appeared
    form: str = Field(min_length=1, max_length=16)  # 10-K | 10-Q | 8-K | ...
    fiscal_year: int | None = None
    fiscal_period: str | None = None  # Q1 | Q2 | Q3 | Q4 | FY
    frame: str | None = None  # SEC's calendar frame, if any

    # Value.
    value: float | None

    @field_validator("fiscal_period", mode="before")
    @classmethod
    def _normalise_fp(cls, v: Any) -> Any:
        """Normalise empty fiscal period strings to None."""
        if isinstance(v, str) and not v.strip():
            return None
        return v


class SecCompanyFacts(BaseModel):
    """All facts for one ticker at one fetch time.

    Attributes:
        ticker: Symbol (uppercase).
        cik: SEC Central Index Key.
        entity_name: Legal entity name as reported by SEC.
        fetched_at: When we pulled this snapshot (our clock).
        facts: List of facts, one per (tag, unit, period, filing).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticker: str = Field(min_length=1, max_length=16)
    cik: int = Field(ge=0)
    entity_name: str = Field(min_length=1)
    fetched_at: datetime = Field(default_factory=utcnow)
    facts: list[SecFact]

    @property
    def n_facts(self) -> int:
        return len(self.facts)

    @property
    def n_non_null(self) -> int:
        return sum(1 for f in self.facts if f.value is not None)

    @property
    def n_tags(self) -> int:
        return len({f.tag for f in self.facts})

    @property
    def first_filed(self) -> date | None:
        if not self.facts:
            return None
        return min(f.filed for f in self.facts)

    @property
    def last_filed(self) -> date | None:
        if not self.facts:
            return None
        return max(f.filed for f in self.facts)


def sec_facts_hash(snapshot: SecCompanyFacts) -> str:
    """Deterministic SHA-256 over a ticker's facts.

    Covers (cik, entity_name, sorted facts). Values are rounded to
    HASH_DECIMALS so that vendor float noise does not break idempotency
    (same rationale as ADR 0006 for prices). `fetched_at` is excluded:
    refetching the same content yields the same hash.

    Args:
        snapshot: The snapshot to hash.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    h = hashlib.sha256()
    h.update(b"sec_store:v1\n")
    h.update(f"cik={snapshot.cik}\n".encode())
    h.update(f"entity_name={snapshot.entity_name}\n".encode())
    h.update(f"n_facts={len(snapshot.facts)}\n".encode())

    # Sort by a stable composite key.
    def key(f: SecFact) -> tuple[str, ...]:
        return (
            f.namespace,
            f.tag,
            f.unit,
            f.period_start.isoformat() if f.period_start else "",
            f.period_end.isoformat(),
            f.filed.isoformat(),
            f.form,
            f.frame or "",
        )

    for f in sorted(snapshot.facts, key=key):
        v = "null" if f.value is None else f"{round(f.value, HASH_DECIMALS):.{HASH_DECIMALS}f}"
        line = "|".join(
            [
                f.namespace,
                f.tag,
                f.unit,
                f.period_start.isoformat() if f.period_start else "",
                f.period_end.isoformat(),
                f.filed.isoformat(),
                f.form,
                f.frame or "",
                v,
            ]
        )
        h.update(line.encode() + b"\n")
    return h.hexdigest()
