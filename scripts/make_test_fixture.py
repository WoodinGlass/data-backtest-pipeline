"""Generate a small deterministic raw-layer fixture for CI.

Reads the local raw layer (produced by `make ingest`), picks a small
subset of tickers and dates, and writes an equivalent Parquet layout
under tests/fixtures/raw/prices/yfinance/.

The fixture is committed to the repo so that CI can run `dbt build`
without hitting the network. It is a *snapshot of the raw layer's
shape*, not of any specific market data — the numbers change only if
the raw layer format changes.

Usage:
    python scripts/make_test_fixture.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import duckdb

# ── Configuration ───────────────────────────────────────────
FIXTURE_TICKERS = ["AAPL", "MSFT", "SPY"]  # SPY needed for benchmark tests
FIXTURE_START = "2024-01-02"
FIXTURE_END = "2024-01-31"

SRC_GLOB = "data/raw/prices/yfinance/*/*.parquet"
DEST_ROOT = Path("tests/fixtures/raw/prices/yfinance")


def main() -> int:
    src = Path("data/raw/prices/yfinance")
    if not src.exists():
        print(f"ERROR: {src} not found. Run `make ingest` first.")
        return 1

    # Clean destination
    if DEST_ROOT.exists():
        shutil.rmtree(DEST_ROOT)
    DEST_ROOT.mkdir(parents=True, exist_ok=True)

    # Read subset from source
    print(f"Reading from {SRC_GLOB}")
    con = duckdb.connect()

    placeholders = ", ".join(f"'{t}'" for t in FIXTURE_TICKERS)
    query = f"""
        SELECT *
        FROM read_parquet('{SRC_GLOB}', union_by_name = true)
        WHERE ticker IN ({placeholders})
          AND date BETWEEN DATE '{FIXTURE_START}' AND DATE '{FIXTURE_END}'
    """
    df = con.sql(query).fetchdf()
    con.close()

    if df.empty:
        print("ERROR: query returned no rows")
        return 1

    total_rows = 0
    for ticker, group in df.groupby("ticker"):
        out_dir = DEST_ROOT / ticker
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "fixture.parquet"
        group.reset_index(drop=True).to_parquet(out_file, index=False)
        n = len(group)
        total_rows += n
        print(f"  {ticker}: {n} rows -> {out_file}")

    # Copy the manifest too, so it is consistent with the fixture
    src_manifest = Path("data/raw/prices/manifest.json")
    dest_manifest_dir = Path("tests/fixtures/raw/prices")
    dest_manifest_dir.mkdir(parents=True, exist_ok=True)
    if src_manifest.exists():
        # Keep only entries for the fixture tickers
        import json
        manifest = json.loads(src_manifest.read_text())
        manifest["tickers"] = {
            t: v for t, v in manifest.get("tickers", {}).items()
            if t in FIXTURE_TICKERS
        }
        (dest_manifest_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True)
        )
        print(f"  manifest.json copied (tickers={sorted(manifest['tickers'])})")

    print(f"\nFixture written: {total_rows} rows across "
          f"{len(FIXTURE_TICKERS)} tickers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
