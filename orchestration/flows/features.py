"""Features flow: build the PIT feature table. ADR 0016 section 4."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from orchestration._compat import flow, task
from orchestration._subprocess import run_command
from orchestration.alerts import Timer
from orchestration.config import OrchestrationSettings

logger = logging.getLogger(__name__)


@task(name="run_build_features", retries=3, retry_delay_seconds=[10, 60, 300])
def _run_build_features(
    extra_args: list[str] | None = None,
    cwd: str | None = None,
) -> dict:
    """Run `python -m features.cli` as a subprocess."""
    cmd = [sys.executable, "-m", "features.cli", *(extra_args or [])]
    with Timer() as t:
        result = run_command(cmd, cwd=cwd, check=True)
    return {
        "returncode": result.returncode,
        "elapsed_ms": t.elapsed_ms,
        "stdout_tail": "\n".join(result.stdout.splitlines()[-5:]),
    }


@flow(name="build_features")
def build_features(
    info_only: bool = False,
    settings: OrchestrationSettings | None = None,
) -> dict:
    """Build the point-in-time feature table (M4).

    info_only : pass `--info` (prints file info, no build)
    """
    if settings is None:
        settings = OrchestrationSettings()
    repo_root = _resolve_repo_root(settings)

    args: list[str] = []
    if info_only:
        args.append("--info")

    logger.info("build_features: repo_root=%s args=%s", repo_root, args)
    fn = getattr(_run_build_features, "fn", _run_build_features)
    return fn(extra_args=args, cwd=str(repo_root))


def _resolve_repo_root(settings: OrchestrationSettings) -> Path:
    if settings.repo_root:
        return Path(settings.repo_root).resolve()
    cur = Path.cwd().resolve()
    for _ in range(15):
        if (cur / ".git").exists() or (cur / "pyproject.toml").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    raise RuntimeError("Could not locate repo root; set DBP_ORCH_REPO_ROOT.")


__all__ = ["build_features"]
