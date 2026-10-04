"""Public flow API for orchestration.

Every pipeline stage (M1-M6) is exposed as a Prefect flow. Each
flow is a thin wrapper around an existing CLI subprocess. No
pipeline logic lives here.

Stage flows:
    ingest_prices, ingest_macro, ingest_sec
    dbt_build
    quality_gate
    build_features
    run_backtest

Composite flows:
    daily_refresh, weekly_refresh, full_refresh

See docs/adr/0016-prefect-orchestration.md.
"""

from orchestration.flows.backtest import run_backtest
from orchestration.flows.composite import (
    daily_refresh,
    full_refresh,
    weekly_refresh,
)
from orchestration.flows.features import build_features
from orchestration.flows.ingest import ingest_macro, ingest_prices, ingest_sec
from orchestration.flows.quality import quality_gate
from orchestration.flows.warehouse import dbt_build

__all__ = [  # noqa: RUF022 (grouped by stage/composite)
    # stage flows
    "build_features",
    "dbt_build",
    "ingest_macro",
    "ingest_prices",
    "ingest_sec",
    "quality_gate",
    "run_backtest",
    # composite flows
    "daily_refresh",
    "full_refresh",
    "weekly_refresh",
]
