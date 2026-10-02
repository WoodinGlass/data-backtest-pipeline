"""Unit tests for ingestion/macro/schemas.py."""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from ingestion.macro.schemas import (
    MacroObservation,
    MacroSnapshot,
    macro_snapshot_hash,
)


# ─── MacroObservation ──────────────────────────────────────
def test_observation_normalises_dot_to_none() -> None:
    o = MacroObservation(
        series_id="FEDFUNDS",
        observation_date=date(2024, 1, 1),
        value=".",
        vintage_date=date(2024, 3, 15),
    )
    assert o.value is None


def test_observation_parses_string_float() -> None:
    o = MacroObservation(
        series_id="FEDFUNDS",
        observation_date=date(2024, 1, 1),
        value="5.33",
        vintage_date=date(2024, 3, 15),
    )
    assert o.value == pytest.approx(5.33)


def test_observation_accepts_none() -> None:
    o = MacroObservation(
        series_id="FEDFUNDS",
        observation_date=date(2024, 1, 1),
        value=None,
        vintage_date=date(2024, 3, 15),
    )
    assert o.value is None


def test_observation_frozen() -> None:
    o = MacroObservation(
        series_id="FEDFUNDS",
        observation_date=date(2024, 1, 1),
        value=5.33,
        vintage_date=date(2024, 3, 15),
    )
    with pytest.raises(ValidationError):
        o.value = 5.0  # type: ignore[misc]


def test_observation_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        MacroObservation(  # type: ignore[call-arg]
            series_id="X",
            observation_date=date(2024, 1, 1),
            value=1.0,
            vintage_date=date(2024, 3, 15),
            extra="nope",
        )


# ─── MacroSnapshot ─────────────────────────────────────────
def _obs(d: date, v: float | None, vd: date = date(2024, 3, 15)) -> MacroObservation:
    return MacroObservation(series_id="FEDFUNDS", observation_date=d, value=v, vintage_date=vd)


def test_snapshot_counts_and_bounds() -> None:
    snap = MacroSnapshot(
        series_id="FEDFUNDS",
        vintage_date=date(2024, 3, 15),
        observations=[
            _obs(date(2024, 1, 1), 5.33),
            _obs(date(2024, 2, 1), None),
            _obs(date(2024, 3, 1), 5.33),
        ],
    )
    assert snap.n_observations == 3
    assert snap.n_non_null == 2
    assert snap.first_date == date(2024, 1, 1)
    assert snap.last_date == date(2024, 3, 1)


def test_snapshot_empty_has_none_bounds() -> None:
    snap = MacroSnapshot(series_id="X", vintage_date=date(2024, 3, 15), observations=[])
    assert snap.first_date is None
    assert snap.last_date is None


# ─── macro_snapshot_hash ───────────────────────────────────
def _snap(obs: list[MacroObservation], vd: date = date(2024, 3, 15)) -> MacroSnapshot:
    return MacroSnapshot(series_id="FEDFUNDS", vintage_date=vd, observations=obs)


def test_hash_is_deterministic() -> None:
    obs = [_obs(date(2024, 1, 1), 5.33), _obs(date(2024, 2, 1), 5.33)]
    h1 = macro_snapshot_hash(_snap(obs))
    h2 = macro_snapshot_hash(_snap(obs))
    assert h1 == h2
    assert len(h1) == 64


def test_hash_insensitive_to_obs_order() -> None:
    obs_a = [_obs(date(2024, 1, 1), 5.33), _obs(date(2024, 2, 1), 5.34)]
    obs_b = list(reversed(obs_a))
    assert macro_snapshot_hash(_snap(obs_a)) == macro_snapshot_hash(_snap(obs_b))


def test_hash_sensitive_to_value() -> None:
    h1 = macro_snapshot_hash(_snap([_obs(date(2024, 1, 1), 5.33)]))
    h2 = macro_snapshot_hash(_snap([_obs(date(2024, 1, 1), 5.34)]))
    assert h1 != h2


def test_hash_sensitive_to_vintage() -> None:
    h1 = macro_snapshot_hash(_snap([_obs(date(2024, 1, 1), 5.33)], vd=date(2024, 3, 15)))
    h2 = macro_snapshot_hash(_snap([_obs(date(2024, 1, 1), 5.33)], vd=date(2024, 4, 1)))
    assert h1 != h2


def test_hash_insensitive_to_fetched_at() -> None:
    obs = [_obs(date(2024, 1, 1), 5.33)]
    s1 = MacroSnapshot(
        series_id="FEDFUNDS",
        vintage_date=date(2024, 3, 15),
        fetched_at=datetime(2024, 3, 16, tzinfo=UTC),
        observations=obs,
    )
    s2 = MacroSnapshot(
        series_id="FEDFUNDS",
        vintage_date=date(2024, 3, 15),
        fetched_at=datetime(2024, 4, 1, tzinfo=UTC),
        observations=obs,
    )
    assert macro_snapshot_hash(s1) == macro_snapshot_hash(s2)


def test_hash_handles_none_value() -> None:
    h1 = macro_snapshot_hash(_snap([_obs(date(2024, 1, 1), None)]))
    h2 = macro_snapshot_hash(_snap([_obs(date(2024, 1, 1), None)]))
    assert h1 == h2
