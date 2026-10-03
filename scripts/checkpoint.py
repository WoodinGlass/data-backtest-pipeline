"""Lightweight checkpoint state for setup_dev.py."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass
class Checkpoint:
    path: Path
    _state: dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "steps": {}}
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {"version": 1, "steps": {}}
        if not isinstance(data, dict):
            return {"version": 1, "steps": {}}
        data.setdefault("steps", {})
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def is_done(self, step: str) -> bool:
        s = self._state["steps"].get(step)
        return bool(s and s.get("status") == "done")

    def start(self, step: str) -> None:
        self._state["steps"][step] = {"status": "running", "started_at": _now()}
        self._save()

    def done(self, step: str, *, meta: dict[str, Any] | None = None) -> None:
        prev = self._state["steps"].get(step) or {}
        self._state["steps"][step] = {
            **prev,
            "status": "done",
            "finished_at": _now(),
            "meta": meta or {},
        }
        self._save()

    def fail(self, step: str, *, error: str) -> None:
        prev = self._state["steps"].get(step) or {}
        self._state["steps"][step] = {
            **prev,
            "status": "failed",
            "finished_at": _now(),
            "error": error[:500],
        }
        self._save()

    def reset(self, step: str | None = None) -> None:
        if step is None:
            self._state["steps"] = {}
        else:
            self._state["steps"].pop(step, None)
        self._save()

    def summary(self) -> str:
        lines = [f"Checkpoint: {self.path}"]
        steps = self._state.get("steps") or {}
        if not steps:
            lines.append("  (no steps recorded)")
        for name, s in sorted(steps.items()):
            status = s.get("status", "?")
            marker = {"done": "OK", "running": "..", "failed": "XX"}.get(status, "?")
            lines.append(f"  [{marker}] {name:20s} {status}")
        return "\n".join(lines)
