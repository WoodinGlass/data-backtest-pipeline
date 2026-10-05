"""Freshness monitor. ADR 0019 section 3.

A single question: how old is the newest row in each mart?

Public API:

    check_mart(df, mart_name, threshold, reference_date) -> dict
    check_all(marts, settings) -> dict
    overall_status(per_mart) -> "PASS" | "WARN" | "FAIL"

A mart DataFrame must have a ``trade_date`` column. Other columns
are ignored. The function is pure: it reads nothing from disk and
writes nothing.

Status rules (calendar days):

    age <= threshold.warn_days           -> PASS
    warn_days < age <= fail_days         -> WARN
    age > threshold.fail_days            -> FAIL

A mart with zero rows is reported as FAIL with reason "empty".
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from monitoring.config import MartThreshold, MonitoringSettings

STATUS_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}

# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _today(reference_date: date | None) -> date:
    """Return reference_date or the wall clock date (UTC)."""
    if reference_date is not None:
        return reference_date
    # Local date is fine for freshness; a one-day skew from UTC
    # does not matter at these thresholds.
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).date()


def _status_for_age(age_days: int, threshold: MartThreshold) -> str:
    if age_days <= threshold.warn_days:
        return "PASS"
    if age_days <= threshold.fail_days:
        return "WARN"
    return "FAIL"


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------


def check_mart(
    df: pd.DataFrame,
    mart_name: str,
    threshold: MartThreshold,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Compute freshness for one mart.

    Returns a dict with keys:
        mart          str
        status        PASS | WARN | FAIL
        rows          int   (total rows in the DataFrame)
        latest_date   ISO date or None
        age_days      int or None
        warn_days     int
        fail_days     int
        reason        short explanation
    """
    today = _today(reference_date)

    base: dict[str, Any] = {
        "mart": mart_name,
        "rows": len(df),
        "warn_days": threshold.warn_days,
        "fail_days": threshold.fail_days,
    }

    if df.empty or "trade_date" not in df.columns:
        base.update(
            {
                "status": "FAIL",
                "latest_date": None,
                "age_days": None,
                "reason": "empty or missing trade_date column",
            }
        )
        return base

    # Coerce to datetime; drop NaN.
    dates = pd.to_datetime(df["trade_date"], errors="coerce").dropna()
    if dates.empty:
        base.update(
            {
                "status": "FAIL",
                "latest_date": None,
                "age_days": None,
                "reason": "no parseable trade_date values",
            }
        )
        return base

    latest = dates.max().date()
    age = (today - latest).days
    status = _status_for_age(age, threshold)

    base.update(
        {
            "status": status,
            "latest_date": latest.isoformat(),
            "age_days": int(age),
            "reason": f"latest={latest.isoformat()} age={age}d "
            f"(warn<={threshold.warn_days}, fail<={threshold.fail_days})",
        }
    )
    return base


# ---------------------------------------------------------------------
# All marts
# ---------------------------------------------------------------------


def check_all(
    marts: dict[str, pd.DataFrame],
    settings: MonitoringSettings | None = None,
) -> dict[str, Any]:
    """Compute freshness for every configured mart.

    ``marts`` maps mart name to DataFrame. Marts in settings but
    absent from ``marts`` are reported as FAIL with reason
    "missing from input".
    """
    if settings is None:
        settings = MonitoringSettings()

    per_mart: list[dict[str, Any]] = []
    for name, threshold in settings.marts.items():
        df = marts.get(name)
        if df is None:
            per_mart.append(
                {
                    "mart": name,
                    "status": "FAIL",
                    "rows": 0,
                    "latest_date": None,
                    "age_days": None,
                    "warn_days": threshold.warn_days,
                    "fail_days": threshold.fail_days,
                    "reason": "missing from input",
                }
            )
            continue
        per_mart.append(
            check_mart(
                df,
                name,
                threshold,
                settings.reference_date,
            )
        )

    return {
        "reference_date": _today(settings.reference_date).isoformat(),
        "marts": per_mart,
        "overall": overall_status(per_mart),
    }


# ---------------------------------------------------------------------
# Overall status
# ---------------------------------------------------------------------


def overall_status(
    per_mart: list[dict[str, Any]],
) -> str:
    """Worst status across all marts. Empty list -> PASS."""
    if not per_mart:
        return "PASS"
    return max(
        (str(m.get("status", "PASS")) for m in per_mart),
        key=lambda s: STATUS_RANK.get(s, 0),
    )


__all__ = [
    "STATUS_RANK",
    "check_all",
    "check_mart",
    "overall_status",
]
