"""Clean up stale raw prices snapshots.

Context: raw_store is append-only by design (M1). Re-ingesting with a
wider window (e.g. extending end date) writes a NEW snapshot per
ticker while the old narrower snapshot remains. Downstream, the union
of both files contains duplicated (ticker, date) pairs.

This script keeps, for each ticker, only the snapshot with:
    - the widest date range (max last_date - first_date), and
    - among ties, the most recently written.

Deletes all other snapshot files for that ticker and updates the
manifest accordingly.

Idempotent: safe to run multiple times.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("data/raw/prices/yfinance")
MANIFEST = Path("data/raw/prices/manifest.json")


def main() -> int:
    if not MANIFEST.exists():
        print(f"ERROR: {MANIFEST} not found")
        return 1

    manifest = json.loads(MANIFEST.read_text())
    tickers = manifest.get("tickers", {})

    total_removed = 0
    n_tickers_cleaned = 0

    for ticker, data in tickers.items():
        snapshots = data.get("snapshots", [])
        if len(snapshots) <= 1:
            continue

        # Pick the snapshot with the latest last_date; tie-break by
        # written_at. ISO date strings compare correctly lexicographically.
        def key(s: dict) -> tuple[str, str]:
            return (s.get("last_date", ""), s.get("written_at", ""))

        keeper = max(snapshots, key=key)
        kept_path = keeper["path"]
        kept_paths = {kept_path}

        # Remove other files
        series_dir = ROOT / ticker
        if series_dir.exists():
            for f in series_dir.glob("*.parquet"):
                rel = f"{ticker}/{f.name}"
                if rel not in kept_paths:
                    f.unlink()
                    total_removed += 1

        data["snapshots"] = [keeper]
        data["latest_hash"] = keeper["hash"]
        n_tickers_cleaned += 1

    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    print(f"  tickers cleaned: {n_tickers_cleaned}")
    print(f"  files removed  : {total_removed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
