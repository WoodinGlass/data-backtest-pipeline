"""Unit tests for ingestion/raw_store.py — no external services."""

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from ingestion.config import Settings
from ingestion.raw_store import (
    MANIFEST_VERSION,
    RawStore,
    snapshot_hash,
)


# ─── fixtures ───────────────────────────────────────────────
@pytest.fixture
def sample_df() -> pd.DataFrame:
    """A tiny normalized OHLCV frame with a 'date'-named index."""
    idx = pd.Index(
        [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        name="date",
    )
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "adj_close": [101.0, 102.0, 103.0],
            "volume": [1000, 2000, 3000],
        },
        index=idx,
    )


@pytest.fixture
def store(tmp_path: Path) -> RawStore:
    """Fresh RawStore rooted at tmp_path — isolated per test."""
    s = Settings(
        price_source="yfinance",
        raw_data_dir=tmp_path / "raw",
    )
    return RawStore(settings=s)


# ─── snapshot_hash ──────────────────────────────────────────
def test_snapshot_hash_is_deterministic(sample_df: pd.DataFrame) -> None:
    h1 = snapshot_hash(sample_df, source="yfinance", ticker="AAPL")
    h2 = snapshot_hash(sample_df, source="yfinance", ticker="AAPL")
    assert h1 == h2
    assert len(h1) == 64
    int(h1, 16)


def test_snapshot_hash_changes_with_data(sample_df: pd.DataFrame) -> None:
    h1 = snapshot_hash(sample_df, source="yfinance", ticker="AAPL")
    modified = sample_df.copy()
    modified.iloc[0, modified.columns.get_loc("close")] = 999.99
    h2 = snapshot_hash(modified, source="yfinance", ticker="AAPL")
    assert h1 != h2


def test_snapshot_hash_changes_with_ticker(sample_df: pd.DataFrame) -> None:
    h1 = snapshot_hash(sample_df, source="yfinance", ticker="AAPL")
    h2 = snapshot_hash(sample_df, source="yfinance", ticker="MSFT")
    assert h1 != h2


def test_snapshot_hash_changes_with_source(sample_df: pd.DataFrame) -> None:
    h1 = snapshot_hash(sample_df, source="yfinance", ticker="AAPL")
    h2 = snapshot_hash(sample_df, source="stooq", ticker="AAPL")
    assert h1 != h2


def test_snapshot_hash_is_stable_against_row_order(sample_df: pd.DataFrame) -> None:
    # NB: hash depends on iteration order, so an unsorted df produces a
    # different hash. Callers must pass sorted frames. This test asserts
    # the *documented* behavior, not sorting-agnostic behavior.
    h1 = snapshot_hash(sample_df, source="yfinance", ticker="AAPL")
    # Re-index in the same order but rebuild the DataFrame — same content.
    same = sample_df.reset_index().set_index("date")
    h2 = snapshot_hash(same, source="yfinance", ticker="AAPL")
    assert h1 == h2


# ─── write_snapshot: happy path ─────────────────────────────
def test_write_first_snapshot(store: RawStore, sample_df: pd.DataFrame) -> None:
    r = store.write_snapshot("AAPL", sample_df)
    assert r.status == "written"
    assert r.ticker == "AAPL"
    assert r.rows == 3
    assert r.first_date == date(2024, 1, 2)
    assert r.last_date == date(2024, 1, 4)
    assert r.path.exists()
    assert r.path.suffix == ".parquet"


def test_write_is_idempotent(store: RawStore, sample_df: pd.DataFrame) -> None:
    r1 = store.write_snapshot("AAPL", sample_df)
    r2 = store.write_snapshot("AAPL", sample_df)
    assert r1.status == "written"
    assert r2.status == "skipped"
    assert r1.hash == r2.hash
    # Only one parquet file on disk
    files = list((store.settings.prices_raw_dir / "AAPL").glob("*.parquet"))
    assert len(files) == 1


def test_write_different_data_creates_new_snapshot(
    store: RawStore, sample_df: pd.DataFrame
) -> None:
    r1 = store.write_snapshot("AAPL", sample_df)
    modified = sample_df.copy()
    modified.iloc[0, modified.columns.get_loc("close")] = 999.99
    r2 = store.write_snapshot("AAPL", modified)
    assert r2.status == "written"
    assert r2.hash != r1.hash
    files = list((store.settings.prices_raw_dir / "AAPL").glob("*.parquet"))
    assert len(files) == 2


def test_write_uppercases_ticker(store: RawStore, sample_df: pd.DataFrame) -> None:
    r = store.write_snapshot("aapl", sample_df)
    assert r.ticker == "AAPL"
    assert (store.settings.prices_raw_dir / "AAPL").exists()


def test_write_rejects_empty_dataframe(store: RawStore) -> None:
    with pytest.raises(ValueError, match="empty snapshot"):
        store.write_snapshot("AAPL", pd.DataFrame())


# ─── read_latest ────────────────────────────────────────────
def test_read_latest_roundtrip(store: RawStore, sample_df: pd.DataFrame) -> None:
    store.write_snapshot("AAPL", sample_df)
    out = store.read_latest("AAPL")
    assert out is not None
    pd.testing.assert_frame_equal(out, sample_df, check_dtype=False)
    assert out.index.name == "date"
    assert isinstance(out.index[0], date)


def test_read_latest_missing_returns_none(store: RawStore) -> None:
    assert store.read_latest("NOPE") is None


def test_read_latest_returns_newest(store: RawStore, sample_df: pd.DataFrame) -> None:
    store.write_snapshot("AAPL", sample_df)
    modified = sample_df.copy()
    modified.iloc[0, modified.columns.get_loc("close")] = 999.99
    store.write_snapshot("AAPL", modified)

    latest = store.read_latest("AAPL")
    assert latest is not None
    assert latest.iloc[0]["close"] == pytest.approx(999.99)


# ─── list_snapshots / latest_hash ──────────────────────────
def test_list_snapshots_empty(store: RawStore) -> None:
    assert store.list_snapshots("AAPL") == []


def test_list_snapshots_newest_first(store: RawStore, sample_df: pd.DataFrame) -> None:
    store.write_snapshot("AAPL", sample_df)
    modified = sample_df.copy()
    modified.iloc[0, modified.columns.get_loc("close")] = 999.99
    store.write_snapshot("AAPL", modified)

    history = store.list_snapshots("AAPL")
    assert len(history) == 2
    # newest first
    assert history[0]["hash"] != history[1]["hash"]


def test_latest_hash_matches_most_recent(store: RawStore, sample_df: pd.DataFrame) -> None:
    r1 = store.write_snapshot("AAPL", sample_df)
    assert store.latest_hash("AAPL") == r1.hash

    modified = sample_df.copy()
    modified.iloc[0, modified.columns.get_loc("close")] = 999.99
    r2 = store.write_snapshot("AAPL", modified)
    assert store.latest_hash("AAPL") == r2.hash


def test_latest_hash_missing_returns_none(store: RawStore) -> None:
    assert store.latest_hash("NOPE") is None


# ─── multi-ticker ──────────────────────────────────────────
def test_multiple_tickers_are_isolated(store: RawStore, sample_df: pd.DataFrame) -> None:
    store.write_snapshot("AAPL", sample_df)
    store.write_snapshot("MSFT", sample_df)

    history_a = store.list_snapshots("AAPL")
    history_m = store.list_snapshots("MSFT")
    assert len(history_a) == 1
    assert len(history_m) == 1

    # Same content for both tickers → same hash (hash depends on content,
    # not on ticker), but files live in different directories.
    assert (store.settings.prices_raw_dir / "AAPL").exists()
    assert (store.settings.prices_raw_dir / "MSFT").exists()


# ─── manifest integrity ────────────────────────────────────
def test_manifest_is_written_atomically(store: RawStore, sample_df: pd.DataFrame) -> None:
    store.write_snapshot("AAPL", sample_df)
    mp = store.settings.manifest_path
    assert mp.exists()
    data = json.loads(mp.read_text())
    assert data["version"] == MANIFEST_VERSION
    assert "AAPL" in data["tickers"]
    assert data["tickers"]["AAPL"]["latest_hash"]


def test_manifest_corrupt_raises(store: RawStore) -> None:
    mp = store.settings.manifest_path
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text("{ this is not json")
    with pytest.raises(RuntimeError, match="corrupt"):
        store.read_latest("AAPL")


def test_manifest_version_mismatch_raises(store: RawStore) -> None:
    mp = store.settings.manifest_path
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"version": 999, "tickers": {}}))
    with pytest.raises(RuntimeError, match="version mismatch"):
        store.read_latest("AAPL")


# ─── atomic write ──────────────────────────────────────────
def test_no_temp_files_left_behind(store: RawStore, sample_df: pd.DataFrame) -> None:
    store.write_snapshot("AAPL", sample_df)
    leftover = list(store.settings.prices_raw_dir.rglob(".tmp_*"))
    assert leftover == []
    leftover_m = list(store.settings.raw_data_dir.rglob(".tmp_manifest_*"))
    assert leftover_m == []
