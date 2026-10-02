"""Generate a small deterministic raw-layer fixture for CI.

Reads the local raw layers (prices + macro) and writes a small subset
under tests/fixtures/raw/. The fixture is committed so CI can run
`dbt build` and the quality gate without network access.

Usage:
    python scripts/make_test_fixture.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import duckdb

# ── Prices fixture ─────────────────────────────────────────
PRICES_TICKERS = ["AAPL", "MSFT", "SPY"]  # SPY needed for benchmark tests
PRICES_START = "2024-01-02"
PRICES_END = "2024-01-31"
PRICES_SRC_GLOB = "data/raw/prices/yfinance/*/*.parquet"
PRICES_DEST_ROOT = Path("tests/fixtures/raw/prices/yfinance")

# ── Macro fixture ──────────────────────────────────────────
# Mix of full-mode (FEDFUNDS, CPIAUCSL, PAYEMS) and latest-mode (DGS10).
MACRO_SERIES = ["FEDFUNDS", "DGS10", "CPIAUCSL", "PAYEMS"]
MACRO_SRC_ROOT = Path("data/raw/macro/fred")
MACRO_DEST_ROOT = Path("tests/fixtures/raw/macro/fred")


def write_prices_fixture() -> int:
    """Copy a small prices subset. Returns total rows written."""
    src = Path("data/raw/prices/yfinance")
    if not src.exists():
        print("ERROR: data/raw/prices/yfinance not found. Run `make ingest` first.")
        return -1

    if PRICES_DEST_ROOT.exists():
        shutil.rmtree(PRICES_DEST_ROOT)
    PRICES_DEST_ROOT.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    placeholders = ", ".join(f"'{t}'" for t in PRICES_TICKERS)
    df = con.sql(
        f"""
        SELECT *
        FROM read_parquet('{PRICES_SRC_GLOB}', union_by_name = true)
        WHERE ticker IN ({placeholders})
          AND date BETWEEN DATE '{PRICES_START}' AND DATE '{PRICES_END}'
        """
    ).fetchdf()
    con.close()

    if df.empty:
        print("ERROR: prices query returned no rows")
        return -1

    total = 0
    for ticker, group in df.groupby("ticker"):
        out_dir = PRICES_DEST_ROOT / ticker
        out_dir.mkdir(parents=True, exist_ok=True)
        group.reset_index(drop=True).to_parquet(out_dir / "fixture.parquet", index=False)
        n = len(group)
        total += n
        print(f"  prices {ticker}: {n} rows")

    # Copy manifest (subset)
    manifest_src = Path("data/raw/prices/manifest.json")
    if manifest_src.exists():
        manifest = json.loads(manifest_src.read_text())
        manifest["tickers"] = {
            t: v for t, v in manifest.get("tickers", {}).items() if t in PRICES_TICKERS
        }
        (PRICES_DEST_ROOT.parent / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True)
        )
    return total


def write_macro_fixture() -> int:
    """Copy the latest vintage of each macro fixture series."""
    if not MACRO_SRC_ROOT.exists():
        print("  WARN: data/raw/macro/fred not found; skipping macro fixture")
        return 0

    if MACRO_DEST_ROOT.exists():
        shutil.rmtree(MACRO_DEST_ROOT)
    MACRO_DEST_ROOT.mkdir(parents=True, exist_ok=True)

    n = 0
    for series_id in MACRO_SERIES:
        sdir = MACRO_SRC_ROOT / series_id
        if not sdir.exists():
            print(f"  macro {series_id}: NOT FOUND (skipping)")
            continue
        files = sorted(sdir.glob("*.parquet"))
        if not files:
            print(f"  macro {series_id}: no snapshots (skipping)")
            continue
        # Latest vintage only (smallest fixture)
        latest = files[-1]
        dest = MACRO_DEST_ROOT / series_id / "fixture.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(latest, dest)
        n += 1
        print(f"  macro {series_id}: {latest.name}")
    return n


def main() -> int:
    print("Generating test fixture...")
    print()
    n_prices = write_prices_fixture()
    if n_prices < 0:
        return 1

    print()
    n_macro = write_macro_fixture()

    print()
    print(
        f"Fixture written: {n_prices} price rows across "
        f"{len(PRICES_TICKERS)} tickers, {n_macro} macro series"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
