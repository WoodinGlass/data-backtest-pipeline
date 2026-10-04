"""Composite flows. ADR 0016 section 5.

Three orchestrated sequences:

    daily_refresh   prices -> dbt(prices) -> quality -> features
    weekly_refresh  macro + sec -> dbt(all) -> quality -> features -> backtest
    full_refresh    prices + macro + sec -> dbt(all) -> quality -> features -> backtest

All three are linear and reuse the stage flows defined in the
sibling modules. No logic is added here: each composite is a
named sequence of calls with shared settings.
"""

from __future__ import annotations

import logging
from typing import Any

from orchestration._compat import flow
from orchestration.config import OrchestrationSettings
from orchestration.flows.backtest import run_backtest
from orchestration.flows.features import build_features
from orchestration.flows.ingest import ingest_macro, ingest_prices, ingest_sec
from orchestration.flows.quality import quality_gate
from orchestration.flows.warehouse import dbt_build

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Helper: call a flow whether or not Prefect wraps it.
# ---------------------------------------------------------------------


def _call(fn, *args, **kwargs):
    """Invoke a flow function. Prefect 3 wraps @flow into an object
    with `.fn`; without Prefect it is a plain function. Either way,
    calling the wrapper works — but for tests we want determinism
    and no Prefect state. Use `.fn` when present.
    """
    inner = getattr(fn, "fn", fn)
    return inner(*args, **kwargs)


# ---------------------------------------------------------------------
# Composite: daily_refresh
# ---------------------------------------------------------------------


@flow(name="daily_refresh")
def daily_refresh(
    settings: OrchestrationSettings | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Daily pipeline: prices -> dbt -> quality -> features.

    No macro, no fundamentals, no backtest. Runs after US market
    close on weekdays. Produces an updated feature table; does not
    produce a new tracked backtest run.
    """
    if settings is None:
        settings = OrchestrationSettings()
    logger.info("daily_refresh: starting")

    r_prices = _call(ingest_prices, start=start, end=end, settings=settings)
    logger.info("daily_refresh: prices done")

    r_dbt = _call(dbt_build, settings=settings)
    logger.info("daily_refresh: dbt done")

    r_qual = _call(
        quality_gate,
        tickers_from_raw=True,
        json_report="reports/quality_daily.json",
        settings=settings,
    )
    logger.info("daily_refresh: quality done")

    r_feat = _call(build_features, settings=settings)
    logger.info("daily_refresh: features done")

    return {
        "flow": "daily_refresh",
        "steps": {
            "prices": r_prices,
            "dbt": r_dbt,
            "quality": r_qual,
            "features": r_feat,
        },
    }


# ---------------------------------------------------------------------
# Composite: weekly_refresh
# ---------------------------------------------------------------------


@flow(name="weekly_refresh")
def weekly_refresh(
    settings: OrchestrationSettings | None = None,
    tracking_uri: str | None = None,
    register_model: bool = True,
) -> dict[str, Any]:
    """Weekly pipeline: macro + sec -> dbt -> quality -> features -> backtest.

    Runs on Sunday night UTC. Produces a new tracked backtest run
    (and, if configured, a new model registry version).
    """
    if settings is None:
        settings = OrchestrationSettings()
    logger.info("weekly_refresh: starting")

    r_macro = _call(ingest_macro, settings=settings)
    logger.info("weekly_refresh: macro done")

    r_sec = _call(ingest_sec, settings=settings)
    logger.info("weekly_refresh: sec done")

    r_dbt = _call(dbt_build, settings=settings)
    logger.info("weekly_refresh: dbt done")

    r_qual = _call(
        quality_gate,
        tickers_from_raw=True,
        json_report="reports/quality_weekly.json",
        settings=settings,
    )
    logger.info("weekly_refresh: quality done")

    r_feat = _call(build_features, settings=settings)
    logger.info("weekly_refresh: features done")

    r_bt = _call(
        run_backtest,
        mlflow=True,
        tracking_uri=tracking_uri,
        register_model=register_model,
        settings=settings,
    )
    logger.info("weekly_refresh: backtest done")

    return {
        "flow": "weekly_refresh",
        "steps": {
            "macro": r_macro,
            "sec": r_sec,
            "dbt": r_dbt,
            "quality": r_qual,
            "features": r_feat,
            "backtest": r_bt,
        },
    }


# ---------------------------------------------------------------------
# Composite: full_refresh
# ---------------------------------------------------------------------


@flow(name="full_refresh")
def full_refresh(
    settings: OrchestrationSettings | None = None,
    start: str | None = None,
    end: str | None = None,
    tracking_uri: str | None = None,
    register_model: bool = True,
) -> dict[str, Any]:
    """Full pipeline: prices + macro + sec -> dbt -> quality -> features -> backtest.

    Manual only. Used for cold start and disaster recovery.
    """
    if settings is None:
        settings = OrchestrationSettings()
    logger.info("full_refresh: starting")

    r_prices = _call(ingest_prices, start=start, end=end, settings=settings)
    logger.info("full_refresh: prices done")

    r_macro = _call(ingest_macro, settings=settings)
    logger.info("full_refresh: macro done")

    r_sec = _call(ingest_sec, settings=settings)
    logger.info("full_refresh: sec done")

    r_dbt = _call(dbt_build, settings=settings)
    logger.info("full_refresh: dbt done")

    r_qual = _call(
        quality_gate,
        tickers_from_raw=True,
        json_report="reports/quality_full.json",
        settings=settings,
    )
    logger.info("full_refresh: quality done")

    r_feat = _call(build_features, settings=settings)
    logger.info("full_refresh: features done")

    r_bt = _call(
        run_backtest,
        mlflow=True,
        tracking_uri=tracking_uri,
        register_model=register_model,
        settings=settings,
    )
    logger.info("full_refresh: backtest done")

    return {
        "flow": "full_refresh",
        "steps": {
            "prices": r_prices,
            "macro": r_macro,
            "sec": r_sec,
            "dbt": r_dbt,
            "quality": r_qual,
            "features": r_feat,
            "backtest": r_bt,
        },
    }


__all__ = [
    "daily_refresh",
    "full_refresh",
    "weekly_refresh",
]
