"""Quality flow: Pandera gate. ADR 0016 section 4."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from orchestration._compat import flow, task
from orchestration._subprocess import run_command
from orchestration.alerts import Timer
from orchestration.config import OrchestrationSettings

logger = logging.getLogger(__name__)


@task(name="run_quality_gate", retries=3, retry_delay_seconds=[10, 60, 300])
def _run_quality_gate(
    extra_args: list[str] | None = None,
    cwd: str | None = None,
) -> dict:
    """Run `python -m quality.cli` as a subprocess."""
    cmd = [sys.executable, "-m", "quality.cli", *(extra_args or [])]
    with Timer() as t:
        result = run_command(cmd, cwd=cwd, check=True)
    return {
        "returncode": result.returncode,
        "elapsed_ms": t.elapsed_ms,
        "stdout_tail": "\n".join(result.stdout.splitlines()[-5:]),
    }


@flow(name="quality_gate")
def quality_gate(
    tickers_from_raw: bool = True,
    json_report: str | None = None,
    skip: list[str] | None = None,
    settings: OrchestrationSettings | None = None,
) -> dict:
    """Run the quality gate over the warehouse (M3, M3.9).

    tickers_from_raw : pass `--tickers-from-raw`
    json_report      : path for `--json` output
    skip             : list of layer names to skip
    """
    if settings is None:
        settings = OrchestrationSettings()
    repo_root = _resolve_repo_root(settings)

    args: list[str] = []
    if tickers_from_raw:
        args.append("--tickers-from-raw")
    if json_report:
        args.extend(["--json", json_report])
    for layer in skip or []:
        args.extend(["--skip", layer])

    logger.info("quality_gate: repo_root=%s args=%s", repo_root, args)
    fn = getattr(_run_quality_gate, "fn", _run_quality_gate)
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


__all__ = ["quality_gate"]
