"""MLflow tracking layer for M6.

Design contract: docs/adr/0015-mlflow-tracking.md

Public API:

    from tracking import TrackingSettings, log_backtest_run

The layer is opt-in. Import failures (MLflow not installed) do not
break the pipeline; the caller controls whether tracking is active.
"""

from tracking.config import TRACKING_VERSION, TrackingSettings

__all__ = ["TRACKING_VERSION", "TrackingSettings"]
