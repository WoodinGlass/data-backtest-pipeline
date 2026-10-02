"""Immutable raw snapshot store for SEC fundamental facts.

Mirrors the design of the prices and macro stores (M1, M3.5):

- **Append-only.** A snapshot, once written, is never modified.
  Corrections become new snapshots.
- **Content-addressed.** Filename includes a SHA-256 prefix of the
  fact set. Refetching the same facts produces the same hash.
- **Atomic.** Temp file + os.replace() prevents partial files.
- **Manifest as index.** One JSON file maps each ticker to its
  snapshot history.

Layout::

    data/raw/fundamentals/
    ├── manifest.json
    └── sec/
        ├── AAPL/
        │   └── CIK0000320193__<hash16>.parquet
        ├── MSFT/
        │   └── CIK0000789019__<hash16>.parquet
        └── ...

See ADR 0010.
"""

from __future__ import annotations

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

from ingestion.logging import get_logger
from ingestion.sec.config import SecSettings, get_sec_settings
from ingestion.sec.schemas import (
    SecCompanyFacts,
    sec_facts_hash,
)

__all__ = [
    "MANIFEST_VERSION",
    "SecRawStore",
    "SecWriteResult",
]

log = get_logger(__name__)

MANIFEST_VERSION = 1
HASH_PREFIX_LEN = 16


@dataclass(frozen=True)
class SecWriteResult:
    """Outcome of a :meth:`SecRawStore.write_snapshot` call."""

    ticker: str
    cik: int
    status: Literal["written", "skipped"]
    hash: str
    path: Path
    n_facts: int
    n_non_null: int
    first_filed: date | None
    last_filed: date | None
    written_at: datetime


@dataclass
class SecRawStore:
    """Manage the immutable raw snapshot layer for SEC facts.

    Args:
        settings: Optional SecSettings override.
        root: Optional override for the snapshot directory root.
        manifest_path: Optional override.
    """

    settings: SecSettings = field(default_factory=get_sec_settings)
    root: Path | None = None
    manifest_path: Path | None = None

    # ── Public API ──────────────────────────────────────────
    def write_snapshot(self, snapshot: SecCompanyFacts) -> SecWriteResult:
        """Write a snapshot if this (ticker, content) is new.

        Idempotent on content: if the manifest already contains an
        entry with the same hash for this ticker, no file is written.

        Args:
            snapshot: The :class:`SecCompanyFacts` to persist.

        Returns:
            :class:`SecWriteResult`.
        """
        ticker = snapshot.ticker
        cik = snapshot.cik
        h = sec_facts_hash(snapshot)
        manifest = self._read_manifest()

        existing = self._find_in_manifest(manifest, ticker, h)
        if existing is not None:
            log.info(
                "sec_snapshot_skipped",
                ticker=ticker,
                cik=cik,
                hash=h[:HASH_PREFIX_LEN],
                reason="content unchanged",
            )
            return SecWriteResult(
                ticker=ticker,
                cik=cik,
                status="skipped",
                hash=h,
                path=self._resolve_root() / existing["path"],
                n_facts=int(existing["n_facts"]),
                n_non_null=int(existing["n_non_null"]),
                first_filed=(
                    date.fromisoformat(existing["first_filed"])
                    if existing.get("first_filed")
                    else None
                ),
                last_filed=(
                    date.fromisoformat(existing["last_filed"])
                    if existing.get("last_filed")
                    else None
                ),
                written_at=datetime.fromisoformat(existing["written_at"]),
            )

        rel_path = f"{ticker}/CIK{cik:010d}__{h[:HASH_PREFIX_LEN]}.parquet"
        abs_path = self._resolve_root() / rel_path
        self._write_parquet(snapshot, abs_path)

        now = datetime.now(tz=UTC)
        entry: dict[str, Any] = {
            "hash": h,
            "cik": cik,
            "path": rel_path,
            "written_at": now.isoformat(),
            "n_facts": snapshot.n_facts,
            "n_non_null": snapshot.n_non_null,
            "n_tags": snapshot.n_tags,
            "first_filed": (snapshot.first_filed.isoformat() if snapshot.first_filed else None),
            "last_filed": (snapshot.last_filed.isoformat() if snapshot.last_filed else None),
        }
        self._append_to_manifest(manifest, ticker=ticker, entry=entry)
        self._write_manifest(manifest)

        log.info(
            "sec_snapshot_written",
            ticker=ticker,
            cik=cik,
            hash=h[:HASH_PREFIX_LEN],
            n_facts=snapshot.n_facts,
            path=str(rel_path),
        )
        return SecWriteResult(
            ticker=ticker,
            cik=cik,
            status="written",
            hash=h,
            path=abs_path,
            n_facts=snapshot.n_facts,
            n_non_null=snapshot.n_non_null,
            first_filed=snapshot.first_filed,
            last_filed=snapshot.last_filed,
            written_at=now,
        )

    def read_latest(self, ticker: str) -> pd.DataFrame | None:
        """Read the most recent snapshot for a ticker, or None."""
        manifest = self._read_manifest()
        entry = self._latest_in_manifest(manifest, ticker.upper())
        if entry is None:
            return None
        abs_path = self._resolve_root() / entry["path"]
        return self._read_parquet(abs_path)

    def list_snapshots(self, ticker: str) -> list[dict[str, Any]]:
        """List all manifest entries for a ticker, newest first."""
        manifest = self._read_manifest()
        tickers = manifest.get("tickers", {})
        entry = tickers.get(ticker.upper())
        if not entry:
            return []
        return list(reversed(entry.get("snapshots", [])))

    def latest_hash(self, ticker: str) -> str | None:
        """Return the latest hash for a ticker, or None."""
        manifest = self._read_manifest()
        entry = self._latest_in_manifest(manifest, ticker.upper())
        return None if entry is None else str(entry["hash"])

    # ── Internals ───────────────────────────────────────────
    def _resolve_root(self) -> Path:
        return self.root if self.root is not None else self.settings.sec_root

    def _resolve_manifest_path(self) -> Path:
        return (
            self.manifest_path
            if self.manifest_path is not None
            else self.settings.sec_manifest_path
        )

    @staticmethod
    def _write_parquet(snapshot: SecCompanyFacts, abs_path: Path) -> None:
        """Write facts to Parquet atomically."""
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        rows = [
            {
                "ticker": f.ticker,
                "cik": f.cik,
                "namespace": f.namespace,
                "tag": f.tag,
                "unit": f.unit,
                "period_start": f.period_start,
                "period_end": f.period_end,
                "filed": f.filed,
                "form": f.form,
                "fiscal_year": f.fiscal_year,
                "fiscal_period": f.fiscal_period,
                "frame": f.frame,
                "value": f.value,
            }
            for f in snapshot.facts
        ]
        df = pd.DataFrame(rows)

        # Enforce types so Parquet schema is stable across writes.
        for col in ("period_start", "period_end", "filed"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col]).dt.date
        df["value"] = pd.to_numeric(df["value"], errors="coerce")

        table = pa.Table.from_pandas(df, preserve_index=False)

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
        table = pq.read_table(abs_path)
        df: pd.DataFrame = cast("pd.DataFrame", table.to_pandas())
        for col in ("period_start", "period_end", "filed"):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col]).dt.date
        return df

    def _read_manifest(self) -> dict[str, Any]:
        mp = self._resolve_manifest_path()
        if not mp.exists():
            return {"version": MANIFEST_VERSION, "tickers": {}}
        try:
            data: dict[str, Any] = cast("dict[str, Any]", json.loads(mp.read_text()))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"SEC manifest is corrupt: {mp}") from exc
        if data.get("version") != MANIFEST_VERSION:
            raise RuntimeError(
                f"SEC manifest version mismatch: {data.get('version')} != {MANIFEST_VERSION}"
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
        entry = manifest.get("tickers", {}).get(ticker, {})
        for snap in entry.get("snapshots", []):
            if snap.get("hash") == h:
                return cast("dict[str, Any]", snap)
        return None

    @staticmethod
    def _latest_in_manifest(manifest: dict[str, Any], ticker: str) -> dict[str, Any] | None:
        entry = manifest.get("tickers", {}).get(ticker, {})
        snaps = entry.get("snapshots", [])
        if not snaps:
            return None
        return cast("dict[str, Any]", snaps[-1])

    @staticmethod
    def _append_to_manifest(
        manifest: dict[str, Any],
        *,
        ticker: str,
        entry: dict[str, Any],
    ) -> None:
        manifest.setdefault("version", MANIFEST_VERSION)
        tickers = manifest.setdefault("tickers", {})
        t = tickers.setdefault(ticker, {"snapshots": []})
        t.setdefault("snapshots", []).append(entry)
        t["latest_hash"] = entry["hash"]
