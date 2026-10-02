"""Unit tests for ingestion/sec/raw_store.py."""

from datetime import date
from pathlib import Path

import pytest

from ingestion.sec.config import SecSettings
from ingestion.sec.raw_store import (
    MANIFEST_VERSION,
    SecRawStore,
)
from ingestion.sec.schemas import SecCompanyFacts, SecFact


def _fact(tag: str = "Revenues", value: float | None = 1.0) -> SecFact:
    return SecFact(
        ticker="AAPL",
        cik=320193,
        namespace="us-gaap",
        tag=tag,
        unit="USD",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 3, 31),
        filed=date(2024, 5, 3),
        form="10-Q",
        fiscal_year=2024,
        fiscal_period="Q2",
        frame="CY2024Q1",
        value=value,
    )


def _snapshot(facts: list[SecFact] | None = None) -> SecCompanyFacts:
    return SecCompanyFacts(
        ticker="AAPL",
        cik=320193,
        entity_name="Apple Inc.",
        facts=facts if facts is not None else [_fact()],
    )


@pytest.fixture
def store(tmp_path: Path) -> SecRawStore:
    return SecRawStore(
        settings=SecSettings(fundamental_raw_data_dir=tmp_path),
        root=tmp_path / "sec",
        manifest_path=tmp_path / "manifest.json",
    )


def test_write_first_snapshot(store: SecRawStore) -> None:
    r = store.write_snapshot(_snapshot())
    assert r.status == "written"
    assert r.ticker == "AAPL"
    assert r.cik == 320193
    assert r.n_facts == 1
    assert r.path.exists()
    assert r.path.suffix == ".parquet"


def test_write_is_idempotent(store: SecRawStore) -> None:
    s = _snapshot()
    r1 = store.write_snapshot(s)
    r2 = store.write_snapshot(s)
    assert r1.status == "written"
    assert r2.status == "skipped"
    assert r1.hash == r2.hash


def test_different_content_creates_new_file(store: SecRawStore) -> None:
    r1 = store.write_snapshot(_snapshot([_fact(value=1.0)]))
    r2 = store.write_snapshot(_snapshot([_fact(value=2.0)]))
    assert r1.status == "written"
    assert r2.status == "written"
    assert r1.hash != r2.hash
    files = list((store.root).glob("AAPL/*.parquet"))  # type: ignore[arg-type]
    assert len(files) == 2


def test_read_latest_round_trip(store: SecRawStore) -> None:
    store.write_snapshot(_snapshot())
    df = store.read_latest("AAPL")
    assert df is not None
    assert len(df) == 1
    assert "tag" in df.columns
    assert df.iloc[0]["tag"] == "Revenues"


def test_read_latest_case_insensitive(store: SecRawStore) -> None:
    store.write_snapshot(_snapshot())
    df = store.read_latest("aapl")
    assert df is not None
    assert len(df) == 1


def test_read_missing_returns_none(store: SecRawStore) -> None:
    assert store.read_latest("NOPE") is None


def test_list_snapshots_newest_first(store: SecRawStore) -> None:
    store.write_snapshot(_snapshot([_fact(value=1.0)]))
    store.write_snapshot(_snapshot([_fact(value=2.0)]))
    hist = store.list_snapshots("AAPL")
    assert len(hist) == 2
    # Newest last appended -> list_snapshots reverses to newest first
    assert hist[0]["n_facts"] == 1


def test_latest_hash(store: SecRawStore) -> None:
    r1 = store.write_snapshot(_snapshot([_fact(value=1.0)]))
    assert store.latest_hash("AAPL") == r1.hash
    r2 = store.write_snapshot(_snapshot([_fact(value=2.0)]))
    assert store.latest_hash("AAPL") == r2.hash


def test_latest_hash_missing_returns_none(store: SecRawStore) -> None:
    assert store.latest_hash("NOPE") is None


def test_manifest_version_mismatch_raises(store: SecRawStore) -> None:
    import json

    mp = store.manifest_path
    assert mp is not None
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"version": 999, "tickers": {}}))
    with pytest.raises(RuntimeError, match="version mismatch"):
        store.read_latest("AAPL")


def test_manifest_corrupt_raises(store: SecRawStore) -> None:
    mp = store.manifest_path
    assert mp is not None
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text("{ not valid json")
    with pytest.raises(RuntimeError, match="corrupt"):
        store.read_latest("AAPL")


def test_no_temp_files_left_behind(store: SecRawStore) -> None:
    store.write_snapshot(_snapshot())
    root = store.root
    assert root is not None
    assert list(root.rglob(".tmp_*")) == []


def test_manifest_has_correct_version(store: SecRawStore) -> None:
    import json

    store.write_snapshot(_snapshot())
    mp = store.manifest_path
    assert mp is not None
    data = json.loads(mp.read_text())
    assert data["version"] == MANIFEST_VERSION
    assert "AAPL" in data["tickers"]
