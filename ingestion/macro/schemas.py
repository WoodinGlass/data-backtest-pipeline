"""Pydantic contracts for the macro ingestion layer.

One observation = one (series_id, observation_date, vintage_date, value).
A snapshot = all observations of one series at one vintage.

Design notes
------------
- ``value`` is ``float | None`` because FRED returns "." for missing
  values (e.g. holidays in daily series). We convert "." to None.
- ``payload_hash`` is computed over the canonical (vintage, observations)
  tuple with values rounded to a coarse precision (same rationale as
  ADR 0006: content identity must be stable against vendor rounding).
- ``fetched_at`` and ``vintage_date`` are distinct: ``vintage_date`` is
  FRED's notion of when this vintage became available, ``fetched_at`` is
  when we pulled it. Both matter for audit.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "MacroObservation",
    "MacroSnapshot",
    "macro_snapshot_hash",
    "utcnow",
]

# FRED returns "." to mean "no value for this period". We normalise to None.
FRED_MISSING = "."

# Rounding used in the content hash. FRED values can carry 4-6 decimals;
# coarser than that rounding would collide, finer would chase vendor noise.
HASH_DECIMALS = 4


def utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(tz=UTC)


class MacroObservation(BaseModel):
    """One observation of one macro series on one vintage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    series_id: str = Field(min_length=1, max_length=64)
    observation_date: date
    value: float | None
    vintage_date: date

    @field_validator("value", mode="before")
    @classmethod
    def _normalise_missing(cls, v: Any) -> Any:
        """Convert FRED's "." sentinel and empty string to None."""
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            if s == FRED_MISSING or s == "":
                return None
            try:
                return float(s)
            except ValueError:
                return None
        return v


class MacroSnapshot(BaseModel):
    """All observations of one series at one vintage, plus audit metadata.

    Attributes:
        series_id: FRED series id, e.g. ``FEDFUNDS``.
        vintage_date: FRED's vintage date (as-of date for this revision).
        fetched_at: When we pulled this snapshot (our clock).
        title: Optional series title, from FRED metadata.
        frequency: Optional FRED frequency code (D, W, M, Q, A).
        units: Optional FRED units code.
        observations: List of observations, one per (observation_date).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    series_id: str = Field(min_length=1, max_length=64)
    vintage_date: date
    fetched_at: datetime = Field(default_factory=utcnow)
    title: str | None = None
    frequency: str | None = None
    units: str | None = None
    observations: list[MacroObservation]

    @property
    def n_observations(self) -> int:
        return len(self.observations)

    @property
    def n_non_null(self) -> int:
        return sum(1 for o in self.observations if o.value is not None)

    @property
    def first_date(self) -> date | None:
        if not self.observations:
            return None
        return min(o.observation_date for o in self.observations)

    @property
    def last_date(self) -> date | None:
        if not self.observations:
            return None
        return max(o.observation_date for o in self.observations)


def macro_snapshot_hash(snapshot: MacroSnapshot) -> str:
    """Deterministic SHA-256 over a snapshot's content.

    The hash covers (series_id, vintage_date, sorted observations).
    Values are rounded to HASH_DECIMALS to tolerate the same vendor
    float noise documented in ADR 0006. ``fetched_at`` is excluded:
    refetching the same vintage at a different wall-clock time produces
    the same hash, which is what makes idempotency work.

    Args:
        snapshot: The snapshot to hash.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    h = hashlib.sha256()
    h.update(b"macro_store:v1\n")
    h.update(f"series_id={snapshot.series_id}\n".encode())
    h.update(f"vintage_date={snapshot.vintage_date.isoformat()}\n".encode())
    # Sort by observation_date for stability regardless of upstream order.
    obs_sorted = sorted(snapshot.observations, key=lambda o: o.observation_date)
    h.update(f"n_obs={len(obs_sorted)}\n".encode())
    for o in obs_sorted:
        v = "null" if o.value is None else f"{round(o.value, HASH_DECIMALS):.{HASH_DECIMALS}f}"
        h.update(f"{o.observation_date.isoformat()}|{v}\n".encode())
    return h.hexdigest()


def _sample_payload() -> dict[str, Any]:
    """Helper for tests and examples: a small in-memory sample."""
    return {
        "series_id": "FEDFUNDS",
        "vintage_date": "2024-03-15",
        "observations": [
            {"observation_date": "2024-01-01", "value": 5.33, "vintage_date": "2024-03-15"},
            {"observation_date": "2024-02-01", "value": 5.33, "vintage_date": "2024-03-15"},
        ],
    }
