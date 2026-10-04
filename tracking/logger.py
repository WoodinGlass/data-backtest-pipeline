"""Log a backtest run to MLflow. ADR 0015 §3-§6.

Public API:

    log_backtest_run(result, summary, run_dir, settings,
                    extra_tags=None, repo_root=None) -> str | None

result   : backtest.runner.RunResult
summary  : dict from backtest.report.summarize
run_dir  : Path to data/backtest/{run_id}/ (M5 artifacts)
settings : TrackingSettings

Returns the MLflow run_id, or None if tracking is off.

Pure-ish: reads M5 artifacts, writes to MLflow. Does not
mutate any input.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from tracking.client import (
    MLFLOW_AVAILABLE,
    get_env_snippet,
    get_git_diff,
    get_git_info,
    start_tracking_run,
)
from tracking.config import TrackingSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Param helpers
# ---------------------------------------------------------------------


def _str(value: Any) -> str:
    """MLflow params must be strings. Normalise without loss."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def _flatten_params(
    prefix: str,
    mapping: dict[str, Any],
) -> dict[str, str]:
    """Prefix keys: {"a": 1} -> {"prefix.a": "1"}."""
    out: dict[str, str] = {}
    for k, v in mapping.items():
        out[prefix + "." + k] = _str(v)
    return out


def collect_params(
    result: Any,
    repo_root: Path | None = None,
) -> dict[str, str]:
    """Build the full param dict for MLflow.

    result is duck-typed: needs .config and .risk_config
    (dicts) from backtest.runner.RunResult.
    """
    params: dict[str, str] = {}
    params.update(_flatten_params("bt", result.config or {}))
    params.update(_flatten_params("risk", result.risk_config or {}))

    git = get_git_info(repo_root)
    params["git.sha"] = git["sha"]
    params["git.branch"] = git["branch"]
    params["git.dirty"] = git["dirty"]

    import platform
    import sys

    params["env.python"] = sys.version.split()[0]
    params["env.platform"] = platform.platform()

    return params


# ---------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------


_POOLED_CLS = ("log_loss", "brier", "auc", "hit_rate")
_POOLED_TRD = (
    "sharpe",
    "cagr",
    "max_drawdown",
    "total_return",
    "avg_daily_turnover",
    "annual_turnover",
    "total_cost_bp",
)
_AGG_CLS = ("auc", "log_loss", "brier", "hit_rate")
_AGG_TRD = ("sharpe", "cagr", "max_drawdown")


def _add_metric(out: dict[str, float], name: str, value: Any) -> None:
    """Add a metric if it is a finite float. Silently skips NaN."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return
    # NaN != NaN; guard explicitly.
    if f != f:
        return
    out[name] = f


def collect_metrics(
    result: Any,
    summary: dict[str, Any],
    settings: TrackingSettings,
) -> dict[str, float]:
    """Build the metric dict for MLflow.

    Namespaces:
        pooled/<k>       — main model pooled metrics
        deflated/<k>     — main model deflated Sharpe
        agg/<k>          — per-fold median / IQR
        baseline/<name>/<k> — one namespace per baseline (pooled)
        fold/<id>/<k>    — per-fold sharpe / auc (optional)
    """
    out: dict[str, float] = {}
    models = summary.get("models", {})

    main = models.get("main", {})
    pooled = main.get("pooled", {})
    cls = pooled.get("classification", {})
    trd = pooled.get("trading", {})
    for k in _POOLED_CLS:
        _add_metric(out, "pooled/" + k, cls.get(k))
    for k in _POOLED_TRD:
        _add_metric(out, "pooled/" + k, trd.get(k))

    ci = pooled.get("sharpe_ci", {})
    _add_metric(out, "pooled/sharpe_ci_lower", ci.get("lower"))
    _add_metric(out, "pooled/sharpe_ci_upper", ci.get("upper"))
    _add_metric(out, "pooled/n_obs", pooled.get("n_obs"))
    _add_metric(out, "pooled/n_days", pooled.get("n_days"))

    dfd = main.get("deflated", {})
    _add_metric(out, "deflated/sharpe_annualized", dfd.get("sharpe_annualized"))
    _add_metric(out, "deflated/deflated_sharpe", dfd.get("deflated_sharpe"))
    _add_metric(out, "deflated/n_trials", dfd.get("n_trials"))

    agg = main.get("aggregate", {})
    for k in _AGG_CLS:
        mi = agg.get("cls_" + k + "_median_iqr", {})
        _add_metric(out, "agg/cls_" + k + "_median", mi.get("median"))
        _add_metric(out, "agg/cls_" + k + "_iqr", mi.get("iqr"))
    for k in _AGG_TRD:
        mi = agg.get("trd_" + k + "_median_iqr", {})
        _add_metric(out, "agg/trd_" + k + "_median", mi.get("median"))
        _add_metric(out, "agg/trd_" + k + "_iqr", mi.get("iqr"))

    # Baselines: only pooled sharpe + auc, to keep UI tidy.
    for name, m in models.items():
        if name == "main":
            continue
        p = m.get("pooled", {})
        bc = p.get("classification", {})
        bt = p.get("trading", {})
        _add_metric(out, "baseline/" + name + "/auc", bc.get("auc"))
        _add_metric(out, "baseline/" + name + "/sharpe", bt.get("sharpe"))

    # Per-fold Sharpe + AUC for main.
    if settings.log_fold_metrics:
        cap = settings.max_fold_metrics
        count = 0
        for fr in getattr(result, "main", []):
            if count >= cap:
                break
            fid = getattr(fr, "fold_id", None)
            if fid is None:
                continue
            pre = "fold/" + str(fid).zfill(2) + "/"
            _add_metric(out, pre + "sharpe", fr.trading.get("sharpe"))
            _add_metric(out, pre + "auc", fr.classification.get("auc"))
            count += 2

    return out


# ---------------------------------------------------------------------
# Artifact helpers
# -----------------------------------------------------------------------


def log_artifacts(
    run_dir: Path,
    settings: TrackingSettings,
    repo_root: Path | None = None,
) -> None:
    """Upload the entire run_dir, plus optional git diff / env."""
    import mlflow  # imported lazily; caller guarantees availability

    if run_dir.exists():
        mlflow.log_artifacts(str(run_dir), artifact_path="backtest")
    else:
        logger.warning("log_artifacts: run_dir does not exist: %s", run_dir)

    if settings.log_git_diff:
        diff = get_git_diff(repo_root, max_bytes=settings.git_diff_max_bytes)
        if diff is not None:
            import tempfile

            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".diff",
                delete=False,
                encoding="utf-8",
            ) as f:
                f.write(diff)
                tmp = f.name
            try:
                mlflow.log_artifact(tmp, artifact_path="meta")
            finally:
                Path(tmp).unlink(missing_ok=True)

    if settings.log_env:
        snip = get_env_snippet(max_lines=settings.env_max_lines)
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(snip)
            tmp = f.name
        try:
            mlflow.log_artifact(tmp, artifact_path="meta")
        finally:
            Path(tmp).unlink(missing_ok=True)


# ---------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------


def log_backtest_run(
    result: Any,
    summary: dict[str, Any],
    run_dir: Path,
    settings: TrackingSettings,
    extra_tags: dict[str, str] | None = None,
    repo_root: Path | None = None,
) -> str | None:
    """Log one backtest run to MLflow. Returns run_id or None."""
    if not settings.enabled:
        logger.debug("log_backtest_run: tracking disabled")
        return None
    if not MLFLOW_AVAILABLE:
        logger.warning(
            "log_backtest_run: tracking enabled but mlflow missing; "
            "no-op. Install with pip install -e .[tracking]."
        )
        return None

    run_name = getattr(result, "run_id", "run")

    tags: dict[str, str] = {
        "mlflow.note.content": "M5 walk-forward backtest",
        "purpose": "walk_forward",
    }
    if extra_tags:
        tags.update(extra_tags)

    with start_tracking_run(
        run_name,
        settings,
        extra_tags=tags,
        repo_root=repo_root,
    ) as run:
        if run is None:
            logger.warning(
                "log_backtest_run: start_tracking_run yielded None "
                "(disabled or experiment setup failed)."
            )
            return None

        import mlflow

        params = collect_params(result, repo_root)
        if params:
            mlflow.log_params(params)
        logger.info("logged %d params", len(params))

        metrics = collect_metrics(result, summary, settings)
        if metrics:
            mlflow.log_metrics(metrics)
        logger.info("logged %d metrics", len(metrics))

        log_artifacts(run_dir, settings, repo_root)
        logger.info("logged artifacts from %s", run_dir)

        return run.info.run_id


__all__ = [
    "collect_metrics",
    "collect_params",
    "log_artifacts",
    "log_backtest_run",
]
