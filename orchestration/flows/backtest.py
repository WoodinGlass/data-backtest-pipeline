"""Backtest flow. ADR 0016 section 4 (simplified).

Wraps scripts/run_backtest.py from M5. Tracking (M6) is a flag on
the script, not a separate flow: --mlflow enables tracking,
--tracking-uri overrides the backend, --no-register-model skips
the registry step. This keeps the flow signature small and
avoids sharing a RunResult object across two flows.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from orchestration._compat import flow, task
from orchestration._subprocess import run_command
from orchestration.alerts import Timer
from orchestration.config import OrchestrationSettings

logger = logging.getLogger(__name__)


@task(name="run_backtest_cli", retries=2, retry_delay_seconds=[30, 120])
def _run_backtest_cli(
    extra_args: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Run `python scripts/run_backtest.py <extra_args>`."""
    cmd = [sys.executable, "scripts/run_backtest.py", *extra_args]
    with Timer() as t:
        result = run_command(cmd, cwd=cwd, env=env, check=True)
    return {
        "returncode": result.returncode,
        "elapsed_ms": t.elapsed_ms,
        "stdout_tail": "\n".join(result.stdout.splitlines()[-10:]),
    }


@flow(name="run_backtest")
def run_backtest(
    features: str | None = None,
    warehouse: str | None = None,
    out_dir: str | None = None,
    bootstrap: int = 1000,
    n_trials: int = 4,
    seed: int | None = None,
    mlflow: bool = True,
    tracking_uri: str | None = None,
    register_model: bool = True,
    settings: OrchestrationSettings | None = None,
) -> dict:
    """Run the walk-forward backtest (M5) with optional tracking (M6).

    features       : --features path (default: feature table from M4)
    warehouse      : --warehouse path (default: data/warehouse.duckdb)
    out_dir        : --out-dir path (default: data/backtest)
    bootstrap      : --bootstrap N (>=100)
    n_trials       : --n-trials N (for deflated Sharpe)
    seed           : --seed N (default: 42 via BacktestSettings)
    mlflow         : enable --mlflow (M6 tracking); default True
    tracking_uri   : --tracking-uri SQLite URI override
    register_model : if False, pass --no-register-model
    """
    if settings is None:
        settings = OrchestrationSettings()
    repo_root = _resolve_repo_root(settings)

    args: list[str] = []
    if features:
        args.extend(["--features", features])
    if warehouse:
        args.extend(["--warehouse", warehouse])
    if out_dir:
        args.extend(["--out-dir", out_dir])
    args.extend(["--bootstrap", str(bootstrap)])
    args.extend(["--n-trials", str(n_trials)])
    if seed is not None:
        args.extend(["--seed", str(seed)])
    args.extend(["--log-level", settings.subprocess_log_level])

    if mlflow:
        args.append("--mlflow")
        if tracking_uri:
            args.extend(["--tracking-uri", tracking_uri])
        if not register_model:
            args.append("--no-register-model")

    logger.info(
        "run_backtest: repo_root=%s mlflow=%s register_model=%s",
        repo_root,
        mlflow,
        register_model,
    )
    fn = getattr(_run_backtest_cli, "fn", _run_backtest_cli)
    return fn(
        extra_args=args,
        cwd=str(repo_root),
        env={"LOG_LEVEL": settings.subprocess_log_level},
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


__all__ = ["run_backtest"]
