"""Performance monitor. ADR 0019 section 6.

Reads the per-fold metrics produced by M5 (metrics.parquet in the
latest backtest run directory) and computes rolling aggregates
over the last ``settings.perf_window_folds`` folds.

Public API:

    check_fold_metrics(df, settings) -> dict
    check_run_dir(run_dir, settings) -> dict
    overall_status(metrics) -> "PASS" | "WARN" | "FAIL"

The metric DataFrame is long-format with columns:

    model      str    ("main" or a baseline name)
    fold_id    int
    cls_auc    float
    cls_log_loss float
    cls_brier  float
    trd_sharpe float
    ...

Only the main model is evaluated by default. Baselines are
ignored here; comparing main to baselines is a backtest-report
concern, not a monitoring concern.

All functions are pure. IO (reading metrics.parquet) lives in
check_run_dir and is the only impure part.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from monitoring.config import MonitoringSettings

STATUS_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}

# Which metric to track, and the (warn, fail) direction.
# Lower is better for log_loss/brier; higher is better for auc/sharpe.
# Thresholds are deliberately loose: monitoring should flag obvious
# degradation, not normal noise. See ADR 0019 section 6.
_TRACKED: dict[str, dict[str, Any]] = {
    "cls_log_loss": {
        "warn": 0.72,  # worse than 0.72 -> WARN
        "fail": 0.75,  # worse than 0.75 -> FAIL
        "direction": "lower_is_better",
    },
    "cls_brier": {
        "warn": 0.26,
        "fail": 0.28,
        "direction": "lower_is_better",
    },
    "cls_auc": {
        "warn": 0.50,  # at or below 0.50 -> WARN
        "fail": 0.48,  # at or below 0.48 -> FAIL
        "direction": "higher_is_better",
    },
    "trd_sharpe": {
        "warn": 0.00,
        "fail": -0.50,
        "direction": "higher_is_better",
    },
}

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _severity(
    value: float,
    warn: float,
    fail: float,
    direction: str,
) -> str:
    """Return PASS | WARN | FAIL for one metric value."""
    if not np.isfinite(value):
        return "PASS"  # missing value is not itself a failure
    if direction == "higher_is_better":
        if value <= fail:
            return "FAIL"
        if value <= warn:
            return "WARN"
        return "PASS"
    # lower_is_better
    if value >= fail:
        return "FAIL"
    if value >= warn:
        return "WARN"
    return "PASS"


def _rolling_mean(
    series: pd.Series,
    window: int,
) -> float:
    """Mean of the last ``window`` values. NaN if not enough data."""
    s = series.dropna()
    if s.empty:
        return float("nan")
    tail = s.tail(window)
    if tail.empty:
        return float("nan")
    return float(tail.mean())


def _worse(a: str, b: str) -> str:
    return a if STATUS_RANK.get(a, 0) >= STATUS_RANK.get(b, 0) else b


# ---------------------------------------------------------------------
# check_fold_metrics
# ---------------------------------------------------------------------


def check_fold_metrics(
    df: pd.DataFrame,
    settings: MonitoringSettings | None = None,
    model: str = "main",
) -> dict[str, Any]:
    """Compute rolling metrics for one model over the last N folds."""
    if settings is None:
        settings = MonitoringSettings()

    out: dict[str, Any] = {
        "model": model,
        "window_folds": settings.perf_window_folds,
        "n_folds_total": 0,
        "n_folds_used": 0,
        "fold_range": None,
        "metrics": {},
        "overall": "PASS",
        "reason": "",
    }

    if df.empty or "model" not in df.columns:
        out["reason"] = "empty or missing model column"
        return out

    sub = df[df["model"] == model].copy()
    if sub.empty:
        out["reason"] = f"no rows for model={model!r}"
        return out

    if "fold_id" in sub.columns:
        sub = sub.sort_values("fold_id")
    out["n_folds_total"] = len(sub)

    window = settings.perf_window_folds
    tail = sub.tail(window)
    out["n_folds_used"] = len(tail)
    if "fold_id" in tail.columns and len(tail) > 0:
        out["fold_range"] = [
            int(tail["fold_id"].iloc[0]),
            int(tail["fold_id"].iloc[-1]),
        ]

    metric_results: dict[str, dict[str, Any]] = {}
    worst = "PASS"
    for col, spec in _TRACKED.items():
        if col not in sub.columns:
            continue
        value = _rolling_mean(sub[col], window)
        sev = _severity(
            value,
            spec["warn"],
            spec["fail"],
            spec["direction"],
        )
        metric_results[col] = {
            "value": round(value, 6) if np.isfinite(value) else None,
            "warn": spec["warn"],
            "fail": spec["fail"],
            "direction": spec["direction"],
            "severity": sev,
        }
        worst = _worse(worst, sev)

    out["metrics"] = metric_results
    out["overall"] = worst
    if metric_results:
        bits = [f"{k}={v['value']}" for k, v in metric_results.items() if v["value"] is not None]
        out["reason"] = ", ".join(bits)
    else:
        out["reason"] = "no tracked metric columns found"
    return out


# ---------------------------------------------------------------------
# check_run_dir
# ---------------------------------------------------------------------


def check_run_dir(
    run_dir: Path | str,
    settings: MonitoringSettings | None = None,
) -> dict[str, Any]:
    """Load metrics.parquet from a run directory and check it.

    Missing file -> dict with overall=FAIL and reason set. That
    is deliberate: a monitored pipeline that has never produced
    a metrics.parquet is a real problem, not a soft skip.
    """
    if settings is None:
        settings = MonitoringSettings()

    run_dir = Path(run_dir)
    metrics_path = run_dir / "metrics.parquet"
    if not metrics_path.exists():
        return {
            "run_dir": str(run_dir),
            "metrics_path": str(metrics_path),
            "model": "main",
            "window_folds": settings.perf_window_folds,
            "n_folds_total": 0,
            "n_folds_used": 0,
            "fold_range": None,
            "metrics": {},
            "overall": "FAIL",
            "reason": "metrics.parquet not found",
        }

    df = pd.read_parquet(metrics_path)
    out = check_fold_metrics(df, settings)
    out["run_dir"] = str(run_dir)
    out["metrics_path"] = str(metrics_path)
    return out


# ---------------------------------------------------------------------
# Overall
# ---------------------------------------------------------------------


def overall_status(metrics: dict[str, Any]) -> str:
    """Worst severity across all metrics in a check result."""
    if not metrics:
        return "PASS"
    per = metrics.get("metrics", {})
    if not per:
        return str(metrics.get("overall", "PASS"))
    return max(
        (str(v.get("severity", "PASS")) for v in per.values()),
        key=lambda s: STATUS_RANK.get(s, 0),
    )


__all__ = [
    "STATUS_RANK",
    "check_fold_metrics",
    "check_run_dir",
    "overall_status",
]
