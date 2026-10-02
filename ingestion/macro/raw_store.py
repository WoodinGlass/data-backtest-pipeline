"""Immutable raw snapshot store for macro series.

Mirrors the prices raw store design (M1) with one twist: the natural
identity of a macro snapshot is ``(series_id, vintage_date)`` — not just
content. Two different vintages of the same series are *both* worth
keeping even when their content happens to overlap, because the
publication date itself carries information.

Layout::

    data/raw/macro/
    ├── manifest.json
    └── fred/
        ├── FEDFUNDS/
        │   ├── 2024-03-15__<hash16>.parquet
        │   └── ...
        └── ...

Filenames include the vintage date so a human can tell snapshots apart
at a glance, and the hash prefix for exact-content deduplication.

See ADR 0009.
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
from ingestion.macro.config import MacroSettings, get_macro_settings
from ingestion.macro.schemas import MacroSnapshot, macro_snapshot_hash

__all__ = [
    "MANIFEST_VERSION",
    "MacroRawStore",
    "MacroWriteResult",
]


log = get_logger(__name__)

MANIFEST_VERSION = 1
HASH_PREFIX_LEN = 16


@dataclass(frozen=True)
class MacroWriteResult:
    """Outcome of a :meth:`MacroRawStore.write_snapshot` call."""

    series_id: str
    vintage_date: date
    status: Literal["written", "skipped"]
    hash: str
    path: Path
    n_observations: int
    n_non_null: int
    written_at: datetime


@dataclass
class MacroRawStore:
    """Manage the immutable raw snapshot layer for macro series.

    Args:
        settings: Optional override; defaults to ``get_macro_settings()``.
        root: Optional override for the snapshot directory root.
        manifest_path: Optional override.
    """

    settings: MacroSettings = field(default_factory=get_macro_settings)
    root: Path | None = None
    manifest_path: Path | None = None

    # ── Public API ──────────────────────────────────────────
    def write_snapshot(self, snapshot: MacroSnapshot) -> MacroWriteResult:
        """Write a snapshot if this (series, vintage, content) is new.

        Idempotent on content: if the manifest already contains an entry
        with the same hash for this (series_id, vintage_date), no file is
        written.

        Args:
            snapshot: The :class:`MacroSnapshot` to persist.

        Returns:
            :class:`MacroWriteResult`.
        """
        series_id = snapshot.series_id
        vintage = snapshot.vintage_date
        h = macro_snapshot_hash(snapshot)
        manifest = self._read_manifest()

        existing = self._find_in_manifest(manifest, series_id, vintage, h)
        if existing is not None:
            log.info(
                "macro_snapshot_skipped",
                series_id=series_id,
                vintage_date=vintage.isoformat(),
                hash=h[:HASH_PREFIX_LEN],
                reason="content unchanged",
            )
            return MacroWriteResult(
                series_id=series_id,
                vintage_date=vintage,
                status="skipped",
                hash=h,
                path=self._resolve_root() / existing["path"],
                n_observations=int(existing["n_observations"]),
                n_non_null=int(existing["n_non_null"]),
                written_at=datetime.fromisoformat(existing["written_at"]),
            )

        rel_path = f"{series_id}/{vintage.isoformat()}__{h[:HASH_PREFIX_LEN]}.parquet"
        abs_path = self._resolve_root() / rel_path
        self._write_parquet(snapshot, abs_path)

        now = datetime.now(tz=UTC)
        entry: dict[str, Any] = {
            "hash": h,
            "vintage_date": vintage.isoformat(),
            "path": rel_path,
            "written_at": now.isoformat(),
            "n_observations": snapshot.n_observations,
            "n_non_null": snapshot.n_non_null,
            "first_date": snapshot.first_date.isoformat() if snapshot.first_date else None,
            "last_date": snapshot.last_date.isoformat() if snapshot.last_date else None,
        }
        self._append_to_manifest(manifest, series_id=series_id, entry=entry)
        self._write_manifest(manifest)

        log.info(
            "macro_snapshot_written",
            series_id=series_id,
            vintage_date=vintage.isoformat(),
            hash=h[:HASH_PREFIX_LEN],
            n_observations=snapshot.n_observations,
            path=str(rel_path),
        )
        return MacroWriteResult(
            series_id=series_id,
            vintage_date=vintage,
            status="written",
            hash=h,
            path=abs_path,
            n_observations=snapshot.n_observations,
            n_non_null=snapshot.n_non_null,
            written_at=now,
        )

    def read_latest(self, series_id: str) -> pd.DataFrame | None:
        """Read the most recent vintage for ``series_id``, or None.

        Returns a DataFrame with columns:
            observation_date, value, vintage_date, series_id
        """
        manifest = self._read_manifest()
        entry = self._latest_in_manifest(manifest, series_id)
        if entry is None:
            return None
        abs_path = self._resolve_root() / entry["path"]
        return self._read_parquet(abs_path)

    def read_vintage(self, series_id: str, vintage_date: date) -> pd.DataFrame | None:
        """Read a specific vintage for ``series_id``, or None."""
        manifest = self._read_manifest()
        entry = self._find_by_vintage(manifest, series_id, vintage_date)
        if entry is None:
            return None
        abs_path = self._resolve_root() / entry["path"]
        return self._read_parquet(abs_path)

    def list_snapshots(self, series_id: str) -> list[dict[str, Any]]:
        """List all manifest entries for ``series_id``, newest first."""
        manifest = self._read_manifest()
        tickers = manifest.get("series", {})
        entry = tickers.get(series_id)
        if not entry:
            return []
        return list(reversed(entry.get("snapshots", [])))

    def latest_vintage(self, series_id: str) -> date | None:
        """Return the newest vintage_date for a series, or None."""
        manifest = self._read_manifest()
        entry = self._latest_in_manifest(manifest, series_id)
        return None if entry is None else date.fromisoformat(entry["vintage_date"])

    # ── Internals ───────────────────────────────────────────
    def _resolve_root(self) -> Path:
        return self.root if self.root is not None else self.settings.fred_root

    def _resolve_manifest_path(self) -> Path:
        return (
            self.manifest_path
            if self.manifest_path is not None
            else self.settings.macro_manifest_path
        )

    @staticmethod
    def _write_parquet(snapshot: MacroSnapshot, abs_path: Path) -> None:
        """Write the snapshot's observations to Parquet atomically."""
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        rows = [
            {
                "series_id": snapshot.series_id,
                "observation_date": o.observation_date,
                "value": o.value,
                "vintage_date": snapshot.vintage_date,
            }
            for o in snapshot.observations
        ]
        df = pd.DataFrame(rows)
        # Enforce dtypes for stable Parquet schema across writes.
        df["observation_date"] = pd.to_datetime(df["observation_date"]).dt.date
        df["vintage_date"] = pd.to_datetime(df["vintage_date"]).dt.date
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
        df["observation_date"] = pd.to_datetime(df["observation_date"]).dt.date
        df["vintage_date"] = pd.to_datetime(df["vintage_date"]).dt.date
        return df.sort_values("observation_date").reset_index(drop=True)

    def _read_manifest(self) -> dict[str, Any]:
        mp = self._resolve_manifest_path()
        if not mp.exists():
            return {"version": MANIFEST_VERSION, "series": {}}
        try:
            data: dict[str, Any] = cast("dict[str, Any]", json.loads(mp.read_text()))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Macro manifest is corrupt: {mp}") from exc
        if data.get("version") != MANIFEST_VERSION:
            raise RuntimeError(
                f"Macro manifest version mismatch: {data.get('version')} != {MANIFEST_VERSION}"
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
    def _find_in_manifest(
        manifest: dict[str, Any], series_id: str, vintage: date, h: str
    ) -> dict[str, Any] | None:
        entries = manifest.get("series", {}).get(series_id, {})
        for snap in entries.get("snapshots", []):
            if snap.get("vintage_date") == vintage.isoformat() and snap.get("hash") == h:
                return cast("dict[str, Any]", snap)
        return None

    @staticmethod
    def _find_by_vintage(
        manifest: dict[str, Any], series_id: str, vintage: date
    ) -> dict[str, Any] | None:
        entries = manifest.get("series", {}).get(series_id, {})
        snaps = entries.get("snapshots", [])
        for snap in reversed(snaps):
            if snap.get("vintage_date") == vintage.isoformat():
                return cast("dict[str, Any]", snap)
        return None

    @staticmethod
    def _latest_in_manifest(manifest: dict[str, Any], series_id: str) -> dict[str, Any] | None:
        entries = manifest.get("series", {}).get(series_id, {})
        snaps = entries.get("snapshots", [])
        if not snaps:
            return None
        return cast("dict[str, Any]", snaps[-1])

    @staticmethod
    def _append_to_manifest(
        manifest: dict[str, Any],
        *,
        series_id: str,
        entry: dict[str, Any],
    ) -> None:
        manifest.setdefault("version", MANIFEST_VERSION)
        series = manifest.setdefault("series", {})
        s = series.setdefault(series_id, {"snapshots": []})
        s.setdefault("snapshots", []).append(entry)
        s["latest_vintage"] = entry["vintage_date"]
