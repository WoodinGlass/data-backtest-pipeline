"""Ingestion flows. ADR 0016 section 4.

Thin Prefect wrappers around the existing ingestion CLIs:

    ingest_prices  -> python -m ingestion.cli
    ingest_macro   -> python -m ingestion.macro.cli
    ingest_sec     -> python -m ingestion.sec.cli

No pipeline logic is reimplemented. Each flow runs one subprocess,
logs the outcome, and returns a small summary dict.
"""

from __future__ import annotations

import logging
from pathlib import Path

from orchestration._compat import flow, task
from orchestration._subprocess import run_command
from orchestration.alerts import Timer
from orchestration.config import OrchestrationSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _resolve_repo_root(settings: OrchestrationSettings) -> Path:
    """Return repo root, honouring settings.repo_root if set."""
    if settings.repo_root:
        return Path(settings.repo_root).resolve()
    cur = Path.cwd().resolve()
    for _ in range(15):
        if (cur / ".git").exists() or (cur / "pyproject.toml").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    raise RuntimeError("Could not locate repo root; set DBP_ORCH_REPO_ROOT explicitly.")


def _env(settings: OrchestrationSettings) -> dict[str, str]:
    """Environment overrides passed to the subprocess."""
    return {
        "LOG_LEVEL": settings.subprocess_log_level,
    }


# ---------------------------------------------------------------------
# Task: run one CLI. Reused by all three flows.
# ---------------------------------------------------------------------


@task(name="run_cli", retries=3, retry_delay_seconds=[10, 60, 300])
def _run_cli(
    module: str,
    extra_args: list[str] | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Run `python -m <module> [extra_args]` as a subprocess."""
    import sys

    cmd = [sys.executable, "-m", module, *(extra_args or [])]
    with Timer() as t:
        result = run_command(
            cmd,
            cwd=cwd or str(Path.cwd()),
            env=env,
            check=True,
        )
    return {
        "module": module,
        "returncode": result.returncode,
        "elapsed_ms": t.elapsed_ms,
        "stdout_tail": "\n".join(result.stdout.splitlines()[-5:]),
    }


# ---------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------


@flow(name="ingest_prices")
def ingest_prices(
    start: str | None = None,
    end: str | None = None,
    settings: OrchestrationSettings | None = None,
) -> dict:
    """Fetch daily OHLCV bars via yfinance (M1)."""
    if settings is None:
        settings = OrchestrationSettings()
    repo_root = _resolve_repo_root(settings)
    args: list[str] = []
    if start:
        args.extend(["--start", start])
    if end:
        args.extend(["--end", end])
    logger.info(
        "ingest_prices: repo_root=%s start=%s end=%s",
        repo_root,
        start or "-",
        end or "-",
    )
    return _run_cli(
        "ingestion.cli",
        extra_args=args,
        cwd=str(repo_root),
        env=_env(settings),
    )


@flow(name="ingest_macro")
def ingest_macro(
    mode: str | None = None,
    settings: OrchestrationSettings | None = None,
) -> dict:
    """Fetch FRED + ALFRED macro series (M3.5)."""
    if settings is None:
        settings = OrchestrationSettings()
    repo_root = _resolve_repo_root(settings)
    args: list[str] = []
    if mode:
        args.extend(["--mode", mode])
    logger.info("ingest_macro: repo_root=%s mode=%s", repo_root, mode or "default")
    return _run_cli(
        "ingestion.macro.cli",
        extra_args=args,
        cwd=str(repo_root),
        env=_env(settings),
    )


@flow(name="ingest_sec")
def ingest_sec(
    settings: OrchestrationSettings | None = None,
) -> dict:
    """Fetch SEC EDGAR XBRL facts (M3.7)."""
    if settings is None:
        settings = OrchestrationSettings()
    repo_root = _resolve_repo_root(settings)
    logger.info("ingest_sec: repo_root=%s", repo_root)
    return _run_cli(
        "ingestion.sec.cli",
        extra_args=[],
        cwd=str(repo_root),
        env=_env(settings),
    )


__all__ = [
    "ingest_macro",
    "ingest_prices",
    "ingest_sec",
]
