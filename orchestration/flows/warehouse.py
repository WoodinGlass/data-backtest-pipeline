"""Warehouse flow: dbt build. ADR 0016 section 4."""

from __future__ import annotations

import logging
from pathlib import Path

from orchestration._compat import flow, task
from orchestration._subprocess import run_command
from orchestration.alerts import Timer
from orchestration.config import OrchestrationSettings

logger = logging.getLogger(__name__)


@task(name="run_dbt_build", retries=3, retry_delay_seconds=[10, 60, 300])
def _run_dbt_build(
    selector: str | None = None,
    vars_json: str | None = None,
    cwd: str | None = None,
    _log_level: str = "INFO",
) -> dict:
    """Run `dbt build` with optional --select and --vars."""
    cmd = ["dbt", "build", "--project-dir", "dbt", "--profiles-dir", "dbt"]
    if selector:
        cmd += ["--select", selector]
    if vars_json:
        cmd += ["--vars", vars_json]
    with Timer() as t:
        result = run_command(cmd, cwd=cwd, check=True)
    return {
        "returncode": result.returncode,
        "elapsed_ms": t.elapsed_ms,
        "stdout_tail": "\n".join(result.stdout.splitlines()[-5:]),
    }


@flow(name="dbt_build")
def dbt_build(
    selector: str | None = None,
    vars_json: str | None = None,
    settings: OrchestrationSettings | None = None,
) -> dict:
    """Build the warehouse via dbt (M2, M3.6, M3.8).

    selector  : optional `--select` expression, e.g. `staging+` or `+fct_prices_daily`
    vars_json : optional `--vars` JSON, e.g. `{"raw_prices_glob": "tests/fixtures/..."}`
    """
    if settings is None:
        settings = OrchestrationSettings()
    repo_root = _resolve_repo_root(settings)
    logger.info(
        "dbt_build: repo_root=%s selector=%s vars=%s",
        repo_root,
        selector or "-",
        "yes" if vars_json else "-",
    )
    return (
        _run_dbt_build.fn(
            selector=selector,
            vars_json=vars_json,
            cwd=str(repo_root),
            _log_level=settings.subprocess_log_level,
        )
        if hasattr(_run_dbt_build, "fn")
        else _run_dbt_build(
            selector=selector,
            vars_json=vars_json,
            cwd=str(repo_root),
            _log_level=settings.subprocess_log_level,
        )
    )


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


__all__ = ["dbt_build"]
