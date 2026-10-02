"""Clean up stale raw macro snapshots.

Context: during early M3.6 development, the vintage reconstruction in
ingestion/macro/client.py was fixed (row-per-vintage -> cumulative).
Files written under the old logic are still on disk, and the manifest
contains both old and new entries. That produces duplicate rows when
dbt reads the raw Parquet glob.

This script:
    1. Deduplicates the manifest: for each (series, vintage_date),
       keeps only the most recent entry (by written_at).
    2. Deletes any Parquet file in data/raw/macro/fred/ that is not
       referenced by the deduplicated manifest.
    3. Writes the deduplicated manifest back.

Idempotent: safe to run multiple times.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path("data/raw/macro/fred")
MANIFEST = Path("data/raw/macro/manifest.json")


def main() -> int:
    if not MANIFEST.exists():
        print(f"ERROR: {MANIFEST} not found")
        return 1

    manifest = json.loads(MANIFEST.read_text())
    series = manifest.get("series", {})

    total_removed = 0
    total_kept = 0
    n_series_deduped = 0

    for series_id, data in series.items():
        snapshots = data.get("snapshots", [])
        if not snapshots:
            continue

        # Group by vintage_date, keep the entry with the latest written_at.
        by_vintage: dict[str, dict] = {}
        for entry in snapshots:
            v = entry["vintage_date"]
            prev = by_vintage.get(v)
            if prev is None or entry.get("written_at", "") > prev.get("written_at", ""):
                by_vintage[v] = entry

        deduped = list(by_vintage.values())
        if len(deduped) < len(snapshots):
            n_series_deduped += 1

        data["snapshots"] = deduped
        kept_paths = {e["path"] for e in deduped}

        # Delete orphan files
        series_dir = ROOT / series_id
        if series_dir.exists():
            for f in series_dir.glob("*.parquet"):
                rel = f"{series_id}/{f.name}"
                if rel not in kept_paths:
                    f.unlink()
                    total_removed += 1
        total_kept += len(kept_paths)

    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    print(f"  series deduped : {n_series_deduped}")
    print(f"  files removed  : {total_removed}")
    print(f"  files kept     : {total_kept}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
