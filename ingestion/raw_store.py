"""Immutable raw snapshot store.

Design principles
-----------------
1. **Append-only.** A snapshot, once written, is never modified.
   Corrections (splits, dividends, restatements) become new snapshots.
2. **Content-addressed.** A snapshot's identity is a SHA-256 over its
   content. Two runs with identical data produce the same hash, so the
   second write is a no-op.
3. **Atomic.** Writes go to a temp file in the same directory, then
   ``os.replace`` into place. A crash cannot produce a partial file.
4. **Manifest as index.** A single JSON file maps each ticker to its
   snapshot history (path + hash + metadata). The filesystem holds the
   bytes; the manifest holds the story.

Layout::

    data/raw/prices/
    ├── manifest.json
    └── yfinance/
        ├── AAPL/
        │   ├── 3f57f2f720b67bdd.parquet
        │   └── a1b2c3d4e5f60718.parquet
        └── MSFT/
            └── ...

Usage::

    from ingestion.raw_store import RawStore

    store = RawStore()
    result = store.write_snapshot("AAPL", df)
    if result.status == "written":
        print("new snapshot:", result.path)
    else:
        print("already had it")

    df = store.read_latest("AAPL")
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, cast

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ingestion.config import Settings, get_settings
from ingestion.logging import get_logger
from ingestion.validation import DataQualityError, validate_ohlcv_frame

__all__ = [
    "HASHED_COLUMNS",
    "MANIFEST_VERSION",
    "DataQualityError",
    "RawStore",
    "WriteResult",
    "snapshot_hash",
]


log = get_logger(__name__)

MANIFEST_VERSION = 1
HASH_PREFIX_LEN = 16  # how many hex chars of the hash to use in the filename

# Floating-point noise: upstream providers (and pandas internals) can
# return the "same" price with tiny sub-tick differences across fetches,
# e.g. 183.40403747558594 vs 183.4040069580078 (diff ~3e-5). We round to
# a precision coarser than that noise, but far finer than any real price
# revision (Yahoo revisions are >= 1e-4). This makes the content hash
# stable for "semantically equal" data without hiding real updates.
PRICE_HASH_DECIMALS = 4

# Which columns participate in the content hash. We deliberately exclude
# `adj_close`: it is a vendor-derived field (close * dividend_factor) and
# upstream recomputes it with slightly different float rounding across
# fetches. Two AAPL bars with identical OHLCV but adj_close differing by
# 1e-4 are *semantically the same bar*. We still store adj_close; we just
# do not hash on it. Adjusted prices are recomputed from raw close +
# corporate actions in the dbt layer.
HASHED_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


# ────────────────────────────────────────────────────────────
# Content hashing
# ────────────────────────────────────────────────────────────
def snapshot_hash(df: pd.DataFrame, *, source: str, ticker: str) -> str:
    """Deterministic SHA-256 over the full content of a snapshot.

    We build the byte stream ourselves rather than relying on
    ``DataFrame.to_csv`` / ``to_json``: pandas formatting has changed
    between versions and would produce different hashes for identical
    data. Our stream is stable across pandas releases.

    Args:
        df: Normalized OHLCV DataFrame (index = date, columns = OHLCV).
        source: Data source identifier, e.g. ``"yfinance"``.
        ticker: Symbol.

    Note:
        The hash deliberately excludes ``adj_close`` (see module-level
        comment on :data:`HASHED_COLUMNS`).

    Returns:
        Hex-encoded SHA-256 digest.
    """
    h = hashlib.sha256()
    h.update(b"raw_store:v3\n")  # v3: hash OHLCV only, exclude adj_close
    h.update(f"source={source}\n".encode())
    h.update(f"ticker={ticker}\n".encode())
    h.update(f"columns={','.join(df.columns)}\n".encode())
    h.update(f"rows={len(df)}\n".encode())
    for idx, row in df.iterrows():
        parts = [str(idx)]
        for col in HASHED_COLUMNS:
            value = row[col]
            if col == "volume":
                parts.append(str(int(value)))
            else:
                rounded = round(float(value), PRICE_HASH_DECIMALS)
                parts.append(f"{rounded:.{PRICE_HASH_DECIMALS}f}")
        h.update("|".join(parts).encode() + b"\n")
    return h.hexdigest()


# ────────────────────────────────────────────────────────────
# Result type
# ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class WriteResult:
    """Outcome of a :meth:`RawStore.write_snapshot` call."""

    ticker: str
    status: Literal["written", "skipped"]
    hash: str
    path: Path
    rows: int
    first_date: date
    last_date: date
    written_at: datetime


# ────────────────────────────────────────────────────────────
# RawStore
# ────────────────────────────────────────────────────────────
@dataclass
class RawStore:
    """Manage the immutable raw snapshot layer.

    Args:
        settings: Optional override; defaults to ``get_settings()``.
        root: Optional override for the snapshot directory root; if
            None, uses ``settings.prices_raw_dir``.
        manifest_path: Optional override; if None, uses
            ``settings.manifest_path``.
    """

    settings: Settings = field(default_factory=get_settings)
    root: Path | None = None
    manifest_path: Path | None = None

    # ── Public API ──────────────────────────────────────────
    def write_snapshot(self, ticker: str, df: pd.DataFrame) -> WriteResult:
        """Write ``df`` as a new snapshot for ``ticker`` if content is new.

        Idempotent: if the manifest already contains a snapshot with the
        same hash for this ticker, no file is written and the existing
        snapshot is returned with ``status="skipped"``.

        Args:
            ticker: Symbol, e.g. ``"AAPL"``. Case-insensitive.
            df: Normalized OHLCV DataFrame.

        Returns:
            A :class:`WriteResult` describing what happened.

        Raises:
            ValueError: if ``df`` is empty or missing required columns.
        """
        if df is None or df.empty:
            raise ValueError(f"Refusing to write empty snapshot for {ticker}")

        ticker = ticker.upper()
        validate_ohlcv_frame(df, ticker=ticker)
        source = self.settings.price_source

        h = snapshot_hash(df, source=source, ticker=ticker)
        manifest = self._read_manifest()

        # Fast path: already have this exact content
        existing = self._find_in_manifest(manifest, ticker, h)
        if existing is not None:
            log.info(
                "snapshot_skipped",
                ticker=ticker,
                hash=h[:HASH_PREFIX_LEN],
                reason="content unchanged",
            )
            return WriteResult(
                ticker=ticker,
                status="skipped",
                hash=h,
                path=self._resolve_root() / ticker / existing["path"],
                rows=int(existing["rows"]),
                first_date=date.fromisoformat(existing["first_date"]),
                last_date=date.fromisoformat(existing["last_date"]),
                written_at=datetime.fromisoformat(existing["written_at"]),
            )

        # Slow path: write the Parquet file, then update the manifest.
        # We add `ticker` as an explicit column so downstream consumers
        # (dbt, features) do not need to infer it from the file path.
        # The content hash is unaffected: it is computed above, before
        # this insertion.
        rel_path = f"{ticker}/{h[:HASH_PREFIX_LEN]}.parquet"
        abs_path = self._resolve_root() / rel_path
        df_to_write = df.copy()
        if "ticker" not in df_to_write.columns:
            df_to_write.insert(0, "ticker", ticker)
        self._write_parquet(df_to_write, abs_path)

        now = datetime.now(tz=UTC)
        entry = {
            "hash": h,
            "path": rel_path,
            "written_at": now.isoformat(),
            "rows": len(df),
            "first_date": df.index[0].isoformat(),
            "last_date": df.index[-1].isoformat(),
        }
        self._append_to_manifest(manifest, source=source, ticker=ticker, entry=entry)
        self._write_manifest(manifest)

        log.info(
            "snapshot_written",
            ticker=ticker,
            hash=h[:HASH_PREFIX_LEN],
            rows=len(df),
            path=str(rel_path),
        )
        return WriteResult(
            ticker=ticker,
            status="written",
            hash=h,
            path=abs_path,
            rows=len(df),
            first_date=df.index[0],
            last_date=df.index[-1],
            written_at=now,
        )

    def read_latest(self, ticker: str) -> pd.DataFrame | None:
        """Return the most recent snapshot for ``ticker``, or None."""
        ticker = ticker.upper()
        manifest = self._read_manifest()
        entry = self._latest_in_manifest(manifest, ticker)
        if entry is None:
            return None
        abs_path = self._resolve_root() / entry["path"]
        return self._read_parquet(abs_path)

    def list_snapshots(self, ticker: str) -> list[dict[str, Any]]:
        """Return all manifest entries for ``ticker``, newest first."""
        ticker = ticker.upper()
        manifest = self._read_manifest()
        tickers = manifest.get("tickers", {})
        entry = tickers.get(ticker)
        if not entry:
            return []
        snapshots: list[dict[str, Any]] = list(entry.get("snapshots", []))
        return list(reversed(snapshots))

    def latest_hash(self, ticker: str) -> str | None:
        """Return the hash of the latest snapshot for ``ticker``, or None."""
        ticker = ticker.upper()
        manifest = self._read_manifest()
        entry = self._latest_in_manifest(manifest, ticker)
        return None if entry is None else str(entry["hash"])

    # ── Internals ───────────────────────────────────────────
    def _resolve_root(self) -> Path:
        return self.root if self.root is not None else self.settings.prices_raw_dir

    def _resolve_manifest_path(self) -> Path:
        return self.manifest_path if self.manifest_path is not None else self.settings.manifest_path

    @staticmethod
    def _write_parquet(df: pd.DataFrame, abs_path: Path) -> None:
        """Write ``df`` to ``abs_path`` atomically."""
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        # Reset index so 'date' becomes a regular column, then cast.
        flat = df.reset_index()
        # Ensure 'date' column exists and is date32-friendly
        if "date" not in flat.columns:
            raise ValueError("DataFrame index must be named 'date'")
        flat["date"] = pd.to_datetime(flat["date"]).dt.date

        table = pa.Table.from_pandas(flat, preserve_index=False)

        # Atomic write: temp file in same dir, then os.replace
        fd, tmp_str = tempfile.mkstemp(prefix=".tmp_", suffix=".parquet", dir=str(abs_path.parent))
        os.close(fd)
        tmp_path = Path(tmp_str)
        try:
            pq.write_table(table, tmp_path, compression="snappy")
            tmp_path.replace(abs_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    @staticmethod
    def _read_parquet(abs_path: Path) -> pd.DataFrame:
        """Read a snapshot Parquet file and restore the date index."""
        table = pq.read_table(abs_path)
        df: pd.DataFrame = cast("pd.DataFrame", table.to_pandas())
        if "date" not in df.columns:
            raise ValueError(f"Snapshot missing 'date' column: {abs_path}")
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.set_index("date").sort_index()
        df.index.name = "date"
        # Drop ticker if present: RawStore's read contract returns OHLCV
        # only, ticker is contextual (caller knows what they asked for).
        if "ticker" in df.columns:
            df = df.drop(columns=["ticker"])
        return df

    def _read_manifest(self) -> dict[str, Any]:
        mp = self._resolve_manifest_path()
        if not mp.exists():
            return {
                "version": MANIFEST_VERSION,
                "source": self.settings.price_source,
                "tickers": {},
            }
        try:
            data: dict[str, Any] = cast("dict[str, Any]", json.loads(mp.read_text()))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Manifest is corrupt: {mp}") from exc
        if data.get("version") != MANIFEST_VERSION:
            raise RuntimeError(
                f"Manifest version mismatch: {data.get('version')} != {MANIFEST_VERSION}"
            )
        return data

    def _write_manifest(self, manifest: dict[str, Any]) -> None:
        mp = self._resolve_manifest_path()
        mp.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_str = tempfile.mkstemp(prefix=".tmp_manifest_", suffix=".json", dir=str(mp.parent))
        os.close(fd)
        tmp_path = Path(tmp_str)
        try:
            tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
            tmp_path.replace(mp)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    @staticmethod
    def _find_in_manifest(manifest: dict[str, Any], ticker: str, h: str) -> dict[str, Any] | None:
        tickers = manifest.get("tickers", {})
        entry = tickers.get(ticker)
        if not entry:
            return None
        for snap in entry.get("snapshots", []):
            if snap.get("hash") == h:
                return cast("dict[str, Any]", snap)
        return None

    @staticmethod
    def _latest_in_manifest(manifest: dict[str, Any], ticker: str) -> dict[str, Any] | None:
        tickers = manifest.get("tickers", {})
        entry = tickers.get(ticker)
        if not entry:
            return None
        snaps = entry.get("snapshots", [])
        if not snaps:
            return None
        return cast("dict[str, Any]", snaps[-1])

    @staticmethod
    def _append_to_manifest(
        manifest: dict[str, Any],
        *,
        source: str,
        ticker: str,
        entry: dict[str, Any],
    ) -> None:
        manifest.setdefault("version", MANIFEST_VERSION)
        manifest.setdefault("source", source)
        tickers = manifest.setdefault("tickers", {})
        ticker_entry = tickers.setdefault(ticker, {"snapshots": []})
        ticker_entry.setdefault("snapshots", []).append(entry)
        ticker_entry["latest_hash"] = entry["hash"]
