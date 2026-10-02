"""Unit tests for ingestion/macro/pipeline.py."""

from datetime import date
from pathlib import Path

import pytest

from ingestion.macro.client import MacroFetchError
from ingestion.macro.config import MacroSeries, MacroSettings
from ingestion.macro.pipeline import (
    MacroIngestResult,
    MacroIngestSummary,
    ingest_macro_series,
    ingest_macro_universe,
)
from ingestion.macro.raw_store import MacroRawStore
from ingestion.macro.schemas import MacroObservation, MacroSnapshot


def _obs(
    d: date,
    v: float | None,
    vd: date,
    series_id: str = "FEDFUNDS",
) -> MacroObservation:
    return MacroObservation(
        series_id=series_id,
        observation_date=d,
        value=v,
        vintage_date=vd,
    )


def _snap(
    vd: date,
    n: int = 2,
    series_id: str = "FEDFUNDS",
) -> MacroSnapshot:
    return MacroSnapshot(
        series_id=series_id,
        vintage_date=vd,
        observations=[_obs(date(2024, 1, 1 + i), 5.0 + i * 0.1, vd, series_id) for i in range(n)],
    )


class FakeClient:
    """Mock client returning canned snapshots per series."""

    def __init__(self, *, snapshots=None, errors=None) -> None:
        self._snapshots = snapshots or {}
        self._errors = errors or {}

    def fetch_vintages(
        self,
        series_id,
        *,
        observation_start,
        observation_end=None,
        mode="full",
    ):
        if series_id in self._errors:
            raise self._errors[series_id]
        return self._snapshots.get(series_id, [])


@pytest.fixture
def store(tmp_path: Path) -> MacroRawStore:
    return MacroRawStore(
        settings=MacroSettings(macro_raw_data_dir=tmp_path),
        root=tmp_path / "fred",
        manifest_path=tmp_path / "manifest.json",
    )


# ─── ingest_macro_series ───────────────────────────────────
def test_ingest_series_writes_snapshots(store: MacroRawStore) -> None:
    client = FakeClient(
        snapshots={"FEDFUNDS": [_snap(date(2024, 3, 15), n=1), _snap(date(2024, 4, 1), n=2)]}
    )
    series = MacroSeries(series_id="FEDFUNDS", title="T", category="rates")
    r = ingest_macro_series(series, start=date(2024, 1, 1), end=None, client=client, store=store)
    assert r.status == "written"
    assert r.n_vintages_written == 2
    assert r.n_observations == 3  # 1 + 2


def test_ingest_series_idempotent(store: MacroRawStore) -> None:
    client = FakeClient(snapshots={"FEDFUNDS": [_snap(date(2024, 3, 15))]})
    series = MacroSeries(series_id="FEDFUNDS", title="T", category="rates")
    r1 = ingest_macro_series(series, start=date(2024, 1, 1), end=None, client=client, store=store)
    r2 = ingest_macro_series(series, start=date(2024, 1, 1), end=None, client=client, store=store)
    assert r1.status == "written"
    assert r2.status == "skipped"
    assert r2.n_vintages_skipped == 1


def test_ingest_series_captures_fetch_error(store: MacroRawStore) -> None:
    client = FakeClient(errors={"BAD": MacroFetchError("nope")})
    series = MacroSeries(series_id="BAD", title="T", category="rates")
    r = ingest_macro_series(series, start=date(2024, 1, 1), end=None, client=client, store=store)
    assert r.status == "failed"
    assert r.error is not None
    assert "nope" in r.error


def test_ingest_series_empty_result_skipped(store: MacroRawStore) -> None:
    client = FakeClient(snapshots={"EMPTY": []})
    series = MacroSeries(series_id="EMPTY", title="T", category="rates")
    r = ingest_macro_series(series, start=date(2024, 1, 1), end=None, client=client, store=store)
    assert r.status == "skipped"
    assert r.error is not None


# ─── ingest_macro_universe ─────────────────────────────────
def test_universe_all_success(store: MacroRawStore) -> None:
    client = FakeClient(
        snapshots={
            "A": [_snap(date(2024, 3, 1), series_id="A")],
            "B": [_snap(date(2024, 3, 1), series_id="B")],
        }
    )
    series = [
        MacroSeries(series_id="A", title="A", category="rates"),
        MacroSeries(series_id="B", title="B", category="rates"),
    ]
    summary = ingest_macro_universe(
        start=date(2024, 1, 1),
        client=client,
        store=store,
        series=series,
    )
    assert len(summary.succeeded) == 2
    assert summary.exit_code == 0


def test_universe_isolates_failures(store: MacroRawStore) -> None:
    client = FakeClient(
        snapshots={"A": [_snap(date(2024, 3, 1), series_id="A")]},
        errors={"B": MacroFetchError("down")},
    )
    series = [
        MacroSeries(series_id="A", title="A", category="rates"),
        MacroSeries(series_id="B", title="B", category="rates"),
    ]
    summary = ingest_macro_universe(
        start=date(2024, 1, 1), client=client, store=store, series=series
    )
    assert len(summary.succeeded) == 1
    assert len(summary.failed) == 1
    assert summary.exit_code == 1


def test_summary_properties() -> None:
    s = MacroIngestSummary(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 1),
        correlation_id="macro-test",
        results=[
            MacroIngestResult("A", "written", n_vintages_written=3, n_observations=10),
            MacroIngestResult("B", "skipped"),
            MacroIngestResult("C", "failed", error="x"),
        ],
    )
    assert len(s.succeeded) == 1
    assert len(s.skipped) == 1
    assert len(s.failed) == 1
    assert s.total_vintages_written == 3
    assert s.total_observations == 10
    assert s.exit_code == 1
