"""Sync macro-related vars in dbt/dbt_project.yml with the YAML registry.

Reads:
  config/macro_series.yml               (all series)
  config/macro_series_latest_only.yml   (latest-mode subset)

Writes:
  macro_series_ids       (all series, sorted)
  macro_latest_mode_ids  (latest-mode subset)

Run after adding or removing a series. Idempotent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    registry_path = root / "config" / "macro_series.yml"
    latest_path = root / "config" / "macro_series_latest_only.yml"
    project_path = root / "dbt" / "dbt_project.yml"

    registry = yaml.safe_load(registry_path.read_text())
    all_ids = sorted(entry["id"] for cat in registry["series"].values() for entry in cat)

    latest_ids: list[str] = []
    if latest_path.exists():
        latest = yaml.safe_load(latest_path.read_text()) or {}
        latest_ids = sorted(set(latest.get("series", [])))

    print(f"Registry: {len(all_ids)} series, {len(latest_ids)} latest-mode")

    src = project_path.read_text()

    def _render(name: str, ids: list[str]) -> str:
        if not ids:
            return f"{name}: []\n"
        return f"{name}:\n" + "".join(f"    - {sid}\n" for sid in ids)

    # Replace or insert macro_series_ids
    old_re = re.compile(r"macro_series_ids:\n(?:    - [A-Z0-9_]+\n)+")
    if not old_re.search(src):
        print("ERROR: macro_series_ids block not found")
        return 1
    src = old_re.sub(_render("macro_series_ids", all_ids), src, count=1)

    # Replace or insert macro_latest_mode_ids
    old_re2 = re.compile(r"macro_latest_mode_ids:\n(?:    - [A-Z0-9_]+\n)*")
    if old_re2.search(src):
        src = old_re2.sub(_render("macro_latest_mode_ids", latest_ids), src, count=1)
    else:
        # insert after macro_series_ids block
        match = re.search(
            r"macro_series_ids:\n(?:    - [A-Z0-9_]+\n)+",
            src,
        )
        if match:
            src = (
                src[: match.end()]
                + "  macro_latest_mode_ids:\n"
                + "".join(f"    - {sid}\n" for sid in latest_ids)
                + src[match.end() :]
            )

    project_path.write_text(src)
    print(f"Updated {project_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
