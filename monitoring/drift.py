"""Drift monitor. ADR 0019 section 4-5.

Two numbers per numeric feature:

    PSI  Population Stability Index (bucketed distribution distance)
    KS   Kolmogorov-Smirnov (max CDF distance + p-value)

A feature is flagged when either PSI crosses the fail threshold
or KS p-value is at or below the fail threshold. Severity is the
worse of the two.

Public API:

    compute_psi(ref, cur, n_bins) -> float
    compute_ks(ref, cur) -> (statistic, p_value)
    check_feature(ref_series, cur_series, thresholds) -> dict
    check_all(ref_df, cur_df, settings) -> dict
    overall_status(features) -> "PASS" | "WARN" | "FAIL"

All functions are pure. No IO.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

from monitoring.config import MonitoringSettings

STATUS_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}

# ---------------------------------------------------------------------
# Population Stability Index
# ---------------------------------------------------------------------


def compute_psi(
    ref: np.ndarray,
    cur: np.ndarray,
    n_bins: int = 10,
) -> float:
    """PSI between reference and current, using quantile bins.

    Bins are defined by reference quantiles so that the reference
    is (nearly) uniform across bins. The same bin edges are then
    applied to the current sample.

    Returns NaN when either sample has fewer than 2 unique values
    (PSI is undefined on degenerate distributions).
    """
    ref = np.asarray(ref, dtype=float)
    cur = np.asarray(cur, dtype=float)
    ref = ref[np.isfinite(ref)]
    cur = cur[np.isfinite(cur)]
    if ref.size < 2 or cur.size < 2:
        return float("nan")

    # Quantile edges from the reference. Use unique to drop
    # degenerate edges (happens when many ties).
    qs = np.linspace(0.0, 1.0, n_bins + 1)
    edges = np.unique(np.quantile(ref, qs))
    if edges.size < 3:
        # Too few distinct quantiles to bin; fall back to NaN.
        return float("nan")

    # Force outer edges to cover both samples.
    edges[0] = min(edges[0], float(np.min(cur)), float(np.min(ref)))
    edges[-1] = max(edges[-1], float(np.max(cur)), float(np.max(ref)))

    ref_counts, _ = np.histogram(ref, bins=edges)
    cur_counts, _ = np.histogram(cur, bins=edges)

    # Convert to proportions; add a small epsilon to avoid log(0).
    eps = 1e-6
    ref_pct = ref_counts / max(ref_counts.sum(), 1)
    cur_pct = cur_counts / max(cur_counts.sum(), 1)
    ref_pct = np.clip(ref_pct, eps, None)
    cur_pct = np.clip(cur_pct, eps, None)

    return float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))


# ---------------------------------------------------------------------
# Kolmogorov-Smirnov
# ---------------------------------------------------------------------


def compute_ks(ref: np.ndarray, cur: np.ndarray) -> tuple[float, float]:
    """Two-sample KS. Returns (statistic, p_value); NaN on degenerate input."""
    ref = np.asarray(ref, dtype=float)
    cur = np.asarray(cur, dtype=float)
    ref = ref[np.isfinite(ref)]
    cur = cur[np.isfinite(cur)]
    if ref.size < 2 or cur.size < 2:
        return (float("nan"), float("nan"))
    res = ks_2samp(ref, cur, alternative="two-sided", method="auto")
    return (float(res.statistic), float(res.pvalue))


# ---------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------


def _psi_severity(psi: float, warn: float, fail: float) -> str:
    if not np.isfinite(psi):
        return "PASS"
    if psi >= fail:
        return "FAIL"
    if psi >= warn:
        return "WARN"
    return "PASS"


def _ks_severity(pvalue: float, warn: float, fail: float) -> str:
    if not np.isfinite(pvalue):
        return "PASS"
    if pvalue <= fail:
        return "FAIL"
    if pvalue <= warn:
        return "WARN"
    return "PASS"


def _worse(a: str, b: str) -> str:
    return a if STATUS_RANK.get(a, 0) >= STATUS_RANK.get(b, 0) else b


# ---------------------------------------------------------------------
# Per-feature check
# ---------------------------------------------------------------------


def check_feature(
    ref_series: pd.Series,
    cur_series: pd.Series,
    settings: MonitoringSettings,
) -> dict[str, Any]:
    """Compute PSI + KS for one feature and return a dict.

    Non-numeric series are returned as PASS with reason
    "non-numeric". A feature with < 2 finite values in either
    window is returned as PASS with reason "insufficient data"
    (drift cannot be measured, so we do not flag).
    """
    name = str(getattr(ref_series, "name", "") or "")
    base: dict[str, Any] = {
        "feature": name,
        "n_ref": 0,
        "n_cur": 0,
        "psi": None,
        "ks_stat": None,
        "ks_pvalue": None,
        "psi_severity": "PASS",
        "ks_severity": "PASS",
        "status": "PASS",
        "reason": "",
    }

    if not pd.api.types.is_numeric_dtype(ref_series) or not pd.api.types.is_numeric_dtype(
        cur_series
    ):
        base["reason"] = "non-numeric"
        return base

    ref = pd.to_numeric(ref_series, errors="coerce").to_numpy(dtype=float)
    cur = pd.to_numeric(cur_series, errors="coerce").to_numpy(dtype=float)
    ref = ref[np.isfinite(ref)]
    cur = cur[np.isfinite(cur)]
    base["n_ref"] = int(ref.size)
    base["n_cur"] = int(cur.size)

    if ref.size < 2 or cur.size < 2:
        base["reason"] = "insufficient data"
        return base

    psi = compute_psi(ref, cur)
    ks_stat, ks_p = compute_ks(ref, cur)

    base["psi"] = round(psi, 6) if np.isfinite(psi) else None
    base["ks_stat"] = round(ks_stat, 6) if np.isfinite(ks_stat) else None
    base["ks_pvalue"] = round(ks_p, 6) if np.isfinite(ks_p) else None

    psi_sev = _psi_severity(psi, settings.drift_psi_warn, settings.drift_psi_fail)
    ks_sev = _ks_severity(ks_p, settings.drift_ks_pvalue_warn, settings.drift_ks_pvalue_fail)
    base["psi_severity"] = psi_sev
    base["ks_severity"] = ks_sev
    base["status"] = _worse(psi_sev, ks_sev)

    bits: list[str] = []
    if np.isfinite(psi):
        bits.append(f"psi={psi:.4f}")
    if np.isfinite(ks_p):
        bits.append(f"ks_p={ks_p:.4g}")
    base["reason"] = ", ".join(bits) if bits else "no signal"
    return base


# ---------------------------------------------------------------------
# All features
# ---------------------------------------------------------------------


def _select_features(
    ref_df: pd.DataFrame,
    settings: MonitoringSettings,
) -> list[str]:
    """Return feature columns (numeric, not excluded)."""
    exclude = set(settings.drift_exclude_columns)
    return [
        c for c in ref_df.columns if c not in exclude and pd.api.types.is_numeric_dtype(ref_df[c])
    ]


def check_all(
    ref_df: pd.DataFrame,
    cur_df: pd.DataFrame,
    settings: MonitoringSettings | None = None,
) -> dict[str, Any]:
    """Compute drift for every numeric feature shared by both frames.

    Features present only in one frame are reported as PASS with
    reason "missing in one window" — we cannot compare them.
    """
    if settings is None:
        settings = MonitoringSettings()

    feature_cols = _select_features(ref_df, settings)
    # Also include numeric features only in cur_df.
    exclude = set(settings.drift_exclude_columns)
    for c in cur_df.columns:
        if c in exclude or c in feature_cols:
            continue
        if pd.api.types.is_numeric_dtype(cur_df[c]):
            feature_cols.append(c)

    per_feature: list[dict[str, Any]] = []
    for name in feature_cols:
        if name not in ref_df.columns or name not in cur_df.columns:
            per_feature.append(
                {
                    "feature": name,
                    "n_ref": int(name in ref_df.columns) * len(ref_df),
                    "n_cur": int(name in cur_df.columns) * len(cur_df),
                    "psi": None,
                    "ks_stat": None,
                    "ks_pvalue": None,
                    "psi_severity": "PASS",
                    "ks_severity": "PASS",
                    "status": "PASS",
                    "reason": "missing in one window",
                }
            )
            continue
        d = check_feature(ref_df[name], cur_df[name], settings)
        d["feature"] = name  # ensure name is set even if Series.name is None
        per_feature.append(d)

    flagged = [d for d in per_feature if d["status"] != "PASS"]
    return {
        "n_features": len(per_feature),
        "flagged_count": len(flagged),
        "features": per_feature,
        "overall": overall_status(per_feature),
    }


# ---------------------------------------------------------------------
# Overall
# ---------------------------------------------------------------------


def overall_status(per_feature: list[dict[str, Any]]) -> str:
    """Worst status across all features. Empty list -> PASS."""
    if not per_feature:
        return "PASS"
    return max(
        (str(d.get("status", "PASS")) for d in per_feature),
        key=lambda s: STATUS_RANK.get(s, 0),
    )


__all__ = [
    "STATUS_RANK",
    "check_all",
    "check_feature",
    "compute_ks",
    "compute_psi",
    "overall_status",
]
