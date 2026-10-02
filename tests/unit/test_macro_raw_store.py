"""Unit tests for ingestion/macro/raw_store.py."""

from datetime import date
from pathlib import Path

import pytest

from ingestion.macro.config import MacroSettings
from ingestion.macro.raw_store import MANIFEST_VERSION, MacroRawStore
from ingestion.macro.schemas import MacroObservation, MacroSnapshot


def _obs(d: date, v: float | None, vd: date) -> MacroObservation:
    return MacroObservation(series_id="FEDFUNDS", observation_date=d, value=v, vintage_date=vd)


def _snap(vd: date, n: int = 3) -> MacroSnapshot:
    return MacroSnapshot(
        series_id="FEDFUNDS",
        vintage_date=vd,
        observations=[_obs(date(2024, 1, 1 + i), 5.0 + i * 0.1, vd) for i in range(n)],
    )


@pytest.fixture
def store(tmp_path: Path) -> MacroRawStore:
    settings = MacroSettings(macro_raw_data_dir=tmp_path)
    return MacroRawStore(
        settings=settings,
        root=tmp_path / "fred",
        manifest_path=tmp_path / "manifest.json",
    )


def test_write_first_snapshot(store: MacroRawStore, tmp_path: Path) -> None:
    snap = _snap(date(2024, 3, 15))
    r = store.write_snapshot(snap)
    assert r.status == "written"
    assert r.series_id == "FEDFUNDS"
    assert r.vintage_date == date(2024, 3, 15)
    assert r.n_observations == 3
    assert r.path.exists()
    assert r.path.suffix == ".parquet"


def test_write_is_idempotent(store: MacroRawStore) -> None:
    snap = _snap(date(2024, 3, 15))
    r1 = store.write_snapshot(snap)
    r2 = store.write_snapshot(snap)
    assert r1.status == "written"
    assert r2.status == "skipped"
    assert r1.hash == r2.hash


def test_different_content_at_same_vintage_creates_new_file(
    store: MacroRawStore,
) -> None:
    snap_a = _snap(date(2024, 3, 15), n=3)
    snap_b = MacroSnapshot(
        series_id="FEDFUNDS",
        vintage_date=date(2024, 3, 15),
        observations=[
            _obs(date(2024, 1, 1 + i), 6.0 + i * 0.1, date(2024, 3, 15)) for i in range(3)
        ],
    )
    r1 = store.write_snapshot(snap_a)
    r2 = store.write_snapshot(snap_b)
    assert r1.status == "written"
    assert r2.status == "written"
    assert r1.hash != r2.hash
    files = list((store.root).glob("FEDFUNDS/*.parquet"))  # type: ignore[arg-type]
    assert len(files) == 2


def test_read_latest_round_trip(store: MacroRawStore) -> None:
    snap = _snap(date(2024, 3, 15), n=3)
    store.write_snapshot(snap)
    df = store.read_latest("FEDFUNDS")
    assert df is not None
    assert len(df) == 3
    assert set(df.columns) == {"series_id", "observation_date", "value", "vintage_date"}
    # Data preserved
    assert df.iloc[0]["observation_date"] == date(2024, 1, 1)
    assert df.iloc[0]["value"] == pytest.approx(5.0)


def test_read_vintage_specific(store: MacroRawStore) -> None:
    store.write_snapshot(_snap(date(2024, 3, 15), n=1))
    store.write_snapshot(_snap(date(2024, 4, 1), n=2))
    df = store.read_vintage("FEDFUNDS", date(2024, 3, 15))
    assert df is not None
    assert len(df) == 1


def test_read_missing_returns_none(store: MacroRawStore) -> None:
    assert store.read_latest("NOPE") is None
    assert store.read_vintage("NOPE", date(2024, 1, 1)) is None


def test_list_snapshots_newest_first(store: MacroRawStore) -> None:
    store.write_snapshot(_snap(date(2024, 3, 15)))
    store.write_snapshot(_snap(date(2024, 4, 1)))
    history = store.list_snapshots("FEDFUNDS")
    assert len(history) == 2
    assert history[0]["vintage_date"] == "2024-04-01"
    assert history[1]["vintage_date"] == "2024-03-15"


def test_latest_vintage(store: MacroRawStore) -> None:
    store.write_snapshot(_snap(date(2024, 3, 15)))
    store.write_snapshot(_snap(date(2024, 4, 1)))
    assert store.latest_vintage("FEDFUNDS") == date(2024, 4, 1)


def test_latest_vintage_missing_returns_none(store: MacroRawStore) -> None:
    assert store.latest_vintage("NOPE") is None


def test_manifest_version_mismatch_raises(store: MacroRawStore) -> None:
    import json

    mp = store.manifest_path
    assert mp is not None
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"version": 999, "series": {}}))
    with pytest.raises(RuntimeError, match="version mismatch"):
        store.read_latest("FEDFUNDS")


def test_manifest_corrupt_raises(store: MacroRawStore) -> None:
    mp = store.manifest_path
    assert mp is not None
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text("{ not valid json")
    with pytest.raises(RuntimeError, match="corrupt"):
        store.read_latest("FEDFUNDS")


def test_no_temp_files_left_behind(store: MacroRawStore) -> None:
    store.write_snapshot(_snap(date(2024, 3, 15)))
    root = store.root
    assert root is not None
    assert list(root.rglob(".tmp_*")) == []


def test_manifest_has_correct_version(store: MacroRawStore) -> None:
    import json

    store.write_snapshot(_snap(date(2024, 3, 15)))
    mp = store.manifest_path
    assert mp is not None
    data = json.loads(mp.read_text())
    assert data["version"] == MANIFEST_VERSION
