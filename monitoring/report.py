"""Assemble the monitoring report. ADR 0019 section 7.

This is the IO edge of monitoring:

    load_marts(warehouse_path, settings)   -> {mart_name: df}
    load_features(features_path)           -> df
    load_latest_run(backtest_dir)          -> Path | None
    build_report(settings, ...)            -> dict
    save_report(report, output_path)       -> Path
    format_summary(report)                 -> str

The three checkers (freshness, drift, performance) are pure.
This module is the only place that reads or writes files.

Failure of one section does not abort the others: each section
is wrapped in try/except and a structured error is recorded in
the report's ``errors`` list.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from monitoring import drift as drift_mod
from monitoring import freshness as freshness_mod
from monitoring import performance as perf_mod
from monitoring.config import MONITORING_VERSION, MonitoringSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Load from warehouse
# ---------------------------------------------------------------------


def load_marts(
    warehouse_path: Path | str,
    settings: MonitoringSettings | None = None,
) -> dict[str, pd.DataFrame]:
    """Load every mart listed in settings.marts from DuckDB.

    Missing mart -> empty DataFrame (check_all will mark it FAIL).
    Missing warehouse file -> every mart is empty.
    """
    if settings is None:
        settings = MonitoringSettings()

    out: dict[str, pd.DataFrame] = {name: pd.DataFrame() for name in settings.marts}

    wh = Path(warehouse_path)
    if not wh.exists():
        logger.warning("warehouse not found: %s", wh)
        return out

    import duckdb

    con = duckdb.connect(str(wh), read_only=True)
    try:
        schema = settings.warehouse_schema.strip()
        for mart_name in settings.marts:
            fq = (schema + "." + mart_name) if schema else mart_name
            try:
                df = con.execute(f"SELECT * FROM {fq}").df()
                # Normalise trade_date to pandas datetime.
                if "trade_date" in df.columns:
                    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
                out[mart_name] = df
                logger.info("loaded %s: %d rows", mart_name, len(df))
            except Exception as e:
                logger.warning("could not load %s: %s", mart_name, e)
                out[mart_name] = pd.DataFrame()
    finally:
        con.close()

    return out


# ---------------------------------------------------------------------
# Load features parquet
# ---------------------------------------------------------------------


def load_features(
    features_path: Path | str,
) -> pd.DataFrame:
    """Load the feature parquet. Missing file -> empty DataFrame."""
    fp = Path(features_path)
    if not fp.exists():
        logger.warning("features parquet not found: %s", fp)
        return pd.DataFrame()
    df = pd.read_parquet(fp)
    if "trade_date" in df.columns:
        df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    logger.info("loaded features: %d rows", len(df))
    return df


# ---------------------------------------------------------------------
# Find latest backtest run
# ---------------------------------------------------------------------


def load_latest_run(backtest_dir: Path | str) -> Path | None:
    """Return the most recent subdirectory of backtest_dir, or None.

    Run dirs are named ``YYYYMMDD_HHMMSS_<tag>``. Lexicographic
    sort == chronological sort at this format.
    """
    bd = Path(backtest_dir)
    if not bd.exists():
        logger.warning("backtest dir not found: %s", bd)
        return None
    runs = [p for p in bd.iterdir() if p.is_dir() and (p / "metrics.parquet").exists()]
    if not runs:
        logger.warning("no run dirs with metrics.parquet under %s", bd)
        return None
    return max(runs, key=lambda p: p.name)


# ---------------------------------------------------------------------
# Drift window selection
# ---------------------------------------------------------------------


def _drift_windows(
    features: pd.DataFrame,
    settings: MonitoringSettings,
) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Return (reference, current) DataFrames, or None if insufficient.

    Reference = first N trading days of the feature table.
    Current   = last M trading days of the feature table.
    Both windows use the same N (= settings.drift_reference_days).
    """
    if features.empty or "trade_date" not in features.columns:
        return None

    n_ref = settings.drift_reference_days
    n_cur = settings.drift_current_days
    if n_cur < n_ref:
        # Use the smaller of the two as the window size for both.
        n_ref = n_cur

    df = features.dropna(subset=["trade_date"]).sort_values("trade_date")
    unique_dates = pd.Index(sorted(df["trade_date"].unique()))
    if len(unique_dates) < 2 * n_ref:
        # Not enough history to have non-overlapping windows.
        logger.warning(
            "drift: only %d unique dates, need >= %d",
            len(unique_dates),
            2 * n_ref,
        )
        return None

    ref_dates = unique_dates[:n_ref]
    cur_dates = unique_dates[-n_ref:]
    ref = df[df["trade_date"].isin(ref_dates)]
    cur = df[df["trade_date"].isin(cur_dates)]
    return ref, cur


# ---------------------------------------------------------------------
# Build report
# ---------------------------------------------------------------------


def build_report(
    settings: MonitoringSettings | None = None,
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    """Assemble the full monitoring report. Best-effort per section."""
    if settings is None:
        settings = MonitoringSettings()

    if repo_root is None:
        # Best-effort: walk up from cwd.
        cur = Path.cwd().resolve()
        for _ in range(15):
            if (cur / "pyproject.toml").exists():
                break
            if cur.parent == cur:
                break
            cur = cur.parent
        repo_root = cur
    repo_root = Path(repo_root)

    errors: list[dict[str, str]] = []
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "version": MONITORING_VERSION,
        "reference_date": (
            settings.reference_date.isoformat()
            if isinstance(settings.reference_date, date)
            else datetime.now(UTC).date().isoformat()
        ),
        "repo_root": str(repo_root),
        "freshness": {},
        "drift": {},
        "performance": {},
        "errors": errors,
        "overall": "PASS",
    }

    # --- Freshness ---
    try:
        marts = load_marts(repo_root / settings.warehouse_path, settings)
        report["freshness"] = freshness_mod.check_all(marts, settings)
    except Exception as e:
        logger.error("freshness failed: %s", e)
        errors.append({"section": "freshness", "error": str(e)})
        report["freshness"] = {"marts": [], "overall": "FAIL"}

    # --- Drift ---
    try:
        features = load_features(repo_root / settings.features_path)
        windows = _drift_windows(features, settings)
        if windows is None:
            report["drift"] = {
                "n_features": 0,
                "flagged_count": 0,
                "features": [],
                "overall": "PASS",
                "reason": "insufficient data for drift windows",
            }
        else:
            ref_df, cur_df = windows
            report["drift"] = drift_mod.check_all(ref_df, cur_df, settings)
    except Exception as e:
        logger.error("drift failed: %s", e)
        errors.append({"section": "drift", "error": str(e)})
        report["drift"] = {"features": [], "overall": "FAIL"}

    # --- Performance ---
    try:
        latest = load_latest_run(repo_root / settings.backtest_dir)
        if latest is None:
            report["performance"] = {
                "run_dir": None,
                "metrics": {},
                "overall": "FAIL",
                "reason": "no backtest run found",
            }
        else:
            report["performance"] = perf_mod.check_run_dir(latest, settings)
    except Exception as e:
        logger.error("performance failed: %s", e)
        errors.append({"section": "performance", "error": str(e)})
        report["performance"] = {"metrics": {}, "overall": "FAIL"}

    # --- Overall ---
    overall_rank = {"PASS": 0, "WARN": 1, "FAIL": 2}
    worst = "PASS"
    for section in ("freshness", "drift", "performance"):
        s = report.get(section, {}).get("overall", "PASS")
        if overall_rank.get(s, 0) > overall_rank.get(worst, 0):
            worst = s
    if errors:
        worst = "FAIL"
    report["overall"] = worst

    return report


# ---------------------------------------------------------------------
# Save report
# ---------------------------------------------------------------------


def save_report(
    report: dict[str, Any],
    output_path: Path | str,
) -> Path:
    """Write the report as JSON. Creates parent dirs as needed."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, indent=2, default=str),
        encoding="utf-8",
    )
    logger.info("wrote monitoring report to %s", out)
    return out


# ---------------------------------------------------------------------
# Text summary
# ---------------------------------------------------------------------


def format_summary(report: dict[str, Any]) -> str:
    """Human-readable summary for stdout."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"Monitoring report — overall {report.get('overall', '?')}")
    lines.append("=" * 72)
    lines.append(f"Generated at:   {report.get('generated_at', '?')}")
    lines.append(f"Reference date: {report.get('reference_date', '?')}")
    lines.append("")

    # Freshness
    fr = report.get("freshness", {})
    lines.append(f"[freshness]  overall={fr.get('overall', '?')}")
    for m in fr.get("marts", []):
        age = m.get("age_days")
        age_s = f"{age}d" if age is not None else "-"
        lines.append(
            f"  {m.get('status', '?'):4s}  {m.get('mart', '?'):26s}  "
            f"age={age_s:>6s}  rows={m.get('rows', 0):>7d}"
        )
    lines.append("")

    # Drift
    dr = report.get("drift", {})
    lines.append(
        f"[drift]      overall={dr.get('overall', '?')}  "
        f"n_features={dr.get('n_features', 0)}  "
        f"flagged={dr.get('flagged_count', 0)}"
    )
    flagged = [d for d in dr.get("features", []) if d.get("status") != "PASS"]
    for d in flagged[:10]:
        lines.append(
            f"  {d.get('status', '?'):4s}  {d.get('feature', '?'):24s}  {d.get('reason', '')}"
        )
    if len(flagged) > 10:
        lines.append(f"  ... ({len(flagged) - 10} more flagged)")
    lines.append("")

    # Performance
    pf = report.get("performance", {})
    lines.append(f"[performance]  overall={pf.get('overall', '?')}")
    lines.append(f"  run_dir: {pf.get('run_dir', '-')}")
    lines.append(
        f"  window:  last {pf.get('n_folds_used', 0)} folds of {pf.get('n_folds_total', 0)}"
    )
    for k, v in sorted(pf.get("metrics", {}).items()):
        lines.append(
            f"  {v.get('severity', '?'):4s}  {k:24s}  "
            f"value={v.get('value')}  (warn={v.get('warn')}, fail={v.get('fail')})"
        )

    # Errors
    errs = report.get("errors", [])
    if errs:
        lines.append("")
        lines.append("[errors]")
        for e in errs:
            lines.append(f"  {e.get('section', '?')}: {e.get('error', '?')}")

    return "\n".join(lines)


__all__ = [
    "build_report",
    "format_summary",
    "load_features",
    "load_latest_run",
    "load_marts",
    "save_report",
]
