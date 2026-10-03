"""Feature assembler.

Combines every feature family into a single wide Parquet artifact,
versioned via ``FEATURE_VERSION``. Downstream (M5 backtest) reads the
Parquet; a thin dbt model (``marts.fct_features_daily``) also loads it
into the warehouse.

Public API:

    assemble_features(returns_df, macro_df, fund_df, settings=...)
        Pure function: run every builder in order, return one frame.

    build_features(settings=..., warehouse_path=...)
        IO wrapper: load sources, call ``assemble_features``, write
        Parquet + manifest.

    load_features(settings=...)
        Read the current version's Parquet.

    feature_content_hash(df)
        SHA-256 over canonical (columns, rows, rounded values). Used
        for idempotency: the same inputs produce the same hash, so a
        re-run is a no-op.

Versioning
----------

Output lives under::

    data/features/<FEATURE_VERSION>/features_daily.parquet

A version bump creates a new directory; old versions stay. See ADR
0012 for rationale.

Only ADR-listed feature families are included: ``px_*`` (prices),
``cs_*`` (cross-sectional), ``mkt_*`` (market), ``mc_*`` (macro),
``fd_*`` (fundamental). Adding a family means adding a builder call
here and bumping ``FEATURE_VERSION`` if downstream consumers are
already training on v1.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb
import numpy as np
import pandas as pd

from features.config import FeatureSettings, get_feature_settings
from features.cross_sectional_features import (
    add_cross_sectional_features,
    cross_sectional_feature_columns,
)
from features.fundamental_features import (
    add_fundamental_features,
    fundamental_feature_columns,
)
from features.macro_features import (
    add_macro_features,
    macro_feature_columns,
)
from features.market_features import (
    add_market_context_features,
    market_feature_columns,
)
from features.price_features import (
    add_price_features,
    price_feature_columns,
)

__all__ = [
    "all_feature_columns",
    "assemble_features",
    "build_features",
    "feature_content_hash",
    "load_features",
]


# Canonical column order in the output frame.
_META_COLUMNS: tuple[str, ...] = ("ticker", "trade_date", "sector")


def all_feature_columns(
    settings: FeatureSettings | None = None,
) -> tuple[str, ...]:
    """Return every feature column name in canonical order."""
    return (
        *price_feature_columns(settings),
        *cross_sectional_feature_columns(settings),
        *market_feature_columns(settings),
        *macro_feature_columns(settings),
        *fundamental_feature_columns(settings),
    )


# ─── pure assembler ─────────────────────────────────────────
def assemble_features(
    returns_df: pd.DataFrame,
    macro_df: pd.DataFrame,
    fund_df: pd.DataFrame,
    *,
    settings: FeatureSettings | None = None,
) -> pd.DataFrame:
    """Run every feature builder and return one wide frame.

    Args:
        returns_df: ``marts.fct_returns_daily`` (features at t plus
            labels at t+1). Labels are preserved in the output so the
            backtest can consume both from a single Parquet.
        macro_df: ``marts.fct_macro_daily`` (wide, vintage-aware).
        fund_df: long-format facts from ``intermediate.int_fundamentals_pit``.
        settings: Optional FeatureSettings override.

    Returns:
        Wide DataFrame sorted by (ticker, trade_date) with all feature
        columns plus the original label columns from ``returns_df``.
    """
    settings = settings or get_feature_settings()

    # Work on a copy to protect the caller's frame.
    df = returns_df.copy()

    # 1. Price features (px_*). Requires adj_close + log_return.
    df = add_price_features(df, settings=settings)

    # 2. Cross-sectional ranks (cs_*). Requires px_ret_{window_medium}d
    #    which was just added, plus sector.
    df = add_cross_sectional_features(df, settings=settings)

    # 3. Market context (mkt_*). Requires log_return + benchmark row.
    df = add_market_context_features(df, settings=settings)

    # 4. Macro (mc_*). Left joins on trade_date.
    df = add_macro_features(df, macro_df, settings=settings)

    # 5. Fundamental (fd_*). Left joins on (ticker, trade_date).
    df = add_fundamental_features(df, fund_df, settings=settings)

    return df.sort_values(["ticker", "trade_date"]).reset_index(drop=True)


# ─── IO wrapper ─────────────────────────────────────────────
def feature_content_hash(df: pd.DataFrame) -> str:
    """Deterministic SHA-256 over a feature frame.

    Covers column names, row count, and per-row values rounded to 6
    decimal places. Rows are sorted by (ticker, trade_date) first so
    that the hash is independent of the caller's ordering.
    """
    ordered = df.sort_values(["ticker", "trade_date"]).reset_index(drop=True)
    h = hashlib.sha256()
    h.update(b"features:v1\n")
    h.update(f"columns={','.join(ordered.columns)}\n".encode())
    h.update(f"rows={len(ordered)}\n".encode())

    for row in ordered.itertuples(index=False):
        parts: list[str] = []
        for value in row:
            if isinstance(value, float):
                if np.isnan(value):
                    parts.append("nan")
                else:
                    parts.append(f"{round(value, 6):.6f}")
            elif value is None or pd.isna(value):
                parts.append("nan")
            else:
                parts.append(str(value))
        h.update("|".join(parts).encode() + b"\n")
    return h.hexdigest()


def _atomic_write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=".tmp_", suffix=".parquet", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_str)
    try:
        # Convert any date objects to pandas datetime for stable schema.
        df_to_write = df.copy()
        if "trade_date" in df_to_write.columns:
            df_to_write["trade_date"] = pd.to_datetime(df_to_write["trade_date"])
        df_to_write.to_parquet(tmp_path, index=False, compression="snappy")
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "entries": []}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Feature manifest corrupt: {path}") from exc
    if not isinstance(data, dict):
        return {"version": 1, "entries": []}
    data.setdefault("entries", [])
    return data


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=".tmp_manifest_", suffix=".json", dir=str(path.parent))
    os.close(fd)
    tmp_path = Path(tmp_str)
    try:
        tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def _load_returns(warehouse_path: Path | str) -> pd.DataFrame:
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return cast(
            "pd.DataFrame",
            con.sql(
                "SELECT ticker, trade_date, sector, adj_close, "
                "log_return, next_log_return, next_return_positive, "
                "is_benchmark "
                "FROM marts.fct_returns_daily "
                "ORDER BY ticker, trade_date"
            ).fetchdf(),
        )
    finally:
        con.close()


def _load_macro(warehouse_path: Path | str) -> pd.DataFrame:
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return cast(
            "pd.DataFrame",
            con.sql("SELECT * FROM marts.fct_macro_daily ORDER BY trade_date").fetchdf(),
        )
    finally:
        con.close()


def _load_fundamentals(warehouse_path: Path | str) -> pd.DataFrame:
    con = duckdb.connect(str(warehouse_path), read_only=True)
    try:
        return cast(
            "pd.DataFrame",
            con.sql(
                "SELECT ticker, trade_date, tag, value FROM intermediate.int_fundamentals_pit"
            ).fetchdf(),
        )
    finally:
        con.close()


def build_features(
    *,
    settings: FeatureSettings | None = None,
    force: bool = False,
) -> Path:
    """Load sources, assemble features, write Parquet + manifest.

    Args:
        settings: Optional FeatureSettings override.
        force: If True, overwrite an existing Parquet even when the
            content hash matches the last recorded entry.

    Returns:
        Path to the written Parquet.

    Raises:
        FileNotFoundError: if the warehouse is missing.
    """
    settings = settings or get_feature_settings()
    wh = settings.warehouse_path
    if not wh.exists():
        raise FileNotFoundError(f"warehouse not found: {wh}")

    print(f"  Loading sources from {wh}...")
    returns_df = _load_returns(wh)
    macro_df = _load_macro(wh)
    fund_df = _load_fundamentals(wh)
    print(f"    returns: {len(returns_df):,} rows")
    print(f"    macro:   {len(macro_df):,} rows")
    print(f"    fund:    {len(fund_df):,} rows")

    print("  Assembling features...")
    assembled = assemble_features(returns_df, macro_df, fund_df, settings=settings)
    h = feature_content_hash(assembled)
    print(f"    shape: {assembled.shape}")
    print(f"    hash:  {h[:16]}...")

    # Checkpoint: if unchanged and not force, skip write.
    manifest = _read_manifest(settings.manifest_path)
    last = manifest["entries"][-1] if manifest["entries"] else None
    if (
        not force
        and last
        and last.get("version") == settings.feature_version
        and last.get("hash") == h
    ):
        print("  Content unchanged from last entry; skipping write.")
        return settings.features_path

    out_path = settings.features_path
    print(f"  Writing {out_path}...")
    _atomic_write_parquet(assembled, out_path)

    entry = {
        "version": settings.feature_version,
        "hash": h,
        "path": str(out_path.relative_to(settings.feature_data_dir)),
        "written_at": datetime.now(tz=UTC).isoformat(),
        "n_rows": len(assembled),
        "n_columns": int(assembled.shape[1]),
        "n_features": len(all_feature_columns(settings)),
        "columns": list(assembled.columns),
    }
    manifest["entries"].append(entry)
    _write_manifest(settings.manifest_path, manifest)
    print(f"  ✅ Wrote {entry['n_rows']:,} rows, {entry['n_columns']} cols")
    return out_path


def load_features(
    *,
    settings: FeatureSettings | None = None,
) -> pd.DataFrame:
    """Read the current version's feature Parquet.

    Raises:
        FileNotFoundError: if the file does not exist.
    """
    settings = settings or get_feature_settings()
    path = settings.features_path
    if not path.exists():
        raise FileNotFoundError(f"Feature file not found: {path}. Run `make features` first.")
    df = pd.read_parquet(path)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df
