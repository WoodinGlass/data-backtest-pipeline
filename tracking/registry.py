"""Refit on all data + register the model. ADR 0015 §7.

Public API:

    refit_full_model(features, bt_settings)
        -> (sklearn.pipeline.Pipeline, list[str] feature_cols)

    register_model(pipeline, feature_cols, sample_x,
                   settings, extra_tags=None, source_run_id=None)
        -> str | None  (model version, e.g. "3")

    log_and_register_model(result, features, bt_settings, settings)
        -> str | None  (model version)

All functions are no-ops (return None) when tracking is disabled
or MLflow is not installed. Refit itself does NOT require MLflow.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from tracking.client import MLFLOW_AVAILABLE, start_tracking_run
from tracking.config import TrackingSettings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Feature column derivation
# ---------------------------------------------------------------------


def _feature_cols(
    features: pd.DataFrame,
    label_column: str,
) -> list[str]:
    """Columns used as model inputs: everything except keys and labels."""
    exclude = {"ticker", "trade_date", "next_return", label_column}
    return [c for c in features.columns if c not in exclude]


# ---------------------------------------------------------------------
# Refit on all data
# ---------------------------------------------------------------------


def refit_full_model(
    features: pd.DataFrame,
    bt_settings: Any,  # backtest.config.BacktestSettings
) -> tuple[Any, list[str]]:
    """Fit a logistic pipeline on all features. No MLflow needed."""
    from backtest.model import build_estimator, compute_time_decay_weights

    if features.empty:
        raise ValueError("refit_full_model: features is empty")

    feature_cols = _feature_cols(features, bt_settings.label_column)

    x = features[feature_cols].to_numpy(dtype=np.float64)
    y = features[bt_settings.label_column].to_numpy().astype(np.int64)

    if bt_settings.use_time_decay:
        w = compute_time_decay_weights(
            features["trade_date"],
            bt_settings.time_decay_half_life_days,
        )
    else:
        w = np.ones(len(features), dtype=float)

    n_classes = len(np.unique(y))
    if n_classes < 2:
        raise ValueError(
            "refit_full_model: training data has only "
            + str(n_classes)
            + " class(es); cannot register a model."
        )

    pipeline = build_estimator(bt_settings)
    pipeline.fit(x, y, clf__sample_weight=w)

    logger.info(
        "refit_full_model: fit on %d rows, %d features",
        len(features),
        len(feature_cols),
    )
    return pipeline, feature_cols


# ---------------------------------------------------------------------
# Model Registry
# ---------------------------------------------------------------------


def register_model(
    pipeline: Any,
    feature_cols: list[str],
    sample_x: pd.DataFrame,
    settings: TrackingSettings,
    extra_tags: dict[str, str] | None = None,
    source_run_id: str | None = None,
) -> str | None:
    """Log + register the pipeline. Returns model version string or None.

    Opens a NEW MLflow run tagged purpose=registry. If source_run_id
    is given, it is recorded as a tag so the registry run can be
    linked back to the walk-forward run.
    """
    if not settings.enabled or not settings.register_model:
        logger.debug("register_model: disabled")
        return None
    if not MLFLOW_AVAILABLE:
        logger.warning(
            "register_model: mlflow not installed; no-op. Install with pip install -e .[tracking]."
        )
        return None

    import mlflow
    from mlflow.models import infer_signature
    from mlflow.tracking import MlflowClient

    try:
        x_sample = sample_x[feature_cols].to_numpy(dtype=np.float64)
        signature = infer_signature(
            x_sample,
            pipeline.predict_proba(x_sample)[:, 1],
        )
    except Exception as e:
        logger.warning("register_model: signature inference failed: %s", e)
        signature = None

    tags: dict[str, str] = {"purpose": "registry"}
    if source_run_id:
        tags["source_run_id"] = source_run_id
    if extra_tags:
        tags.update(extra_tags)

    run_name = "registry_" + (source_run_id or "unknown")

    with start_tracking_run(run_name, settings, extra_tags=tags) as run:
        if run is None:
            return None

        mlflow.log_params(
            {
                "n_features": str(len(feature_cols)),
                "model_name": settings.model_name,
                "source_run_id": source_run_id or "",
            }
        )
        mlflow.log_dict(
            {"feature_cols": feature_cols},
            "feature_cols.json",
        )

        mlflow.sklearn.log_model(
            sk_model=pipeline,
            name="model",
            signature=signature,
            input_example=sample_x[feature_cols].head(5).to_numpy(dtype=np.float64),
            registered_model_name=settings.model_name,
            # MLflow 3.x default serialization (skops) rejects
            # numpy.dtype unless whitelisted. Our models are first-
            # party; whitelisting numpy.dtype is safe.
            skops_trusted_types=["numpy.dtype"],
        )

    # Move @challenger alias to the newest version.
    client = MlflowClient()
    try:
        versions = client.search_model_versions("name='" + settings.model_name + "'")
        if not versions:
            logger.warning("register_model: no versions found after log")
            return None
        latest = max(versions, key=lambda v: int(v.version))
        client.set_registered_model_alias(
            settings.model_name,
            settings.challenger_alias,
            latest.version,
        )
        logger.info(
            "register_model: registered %s v%s; %s -> v%s",
            settings.model_name,
            latest.version,
            settings.challenger_alias,
            latest.version,
        )
        return str(latest.version)
    except Exception as e:
        logger.warning("register_model: alias setup failed: %s", e)
        return None


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------


def log_and_register_model(
    result: Any,  # backtest.runner.RunResult
    features: pd.DataFrame,
    bt_settings: Any,  # backtest.config.BacktestSettings
    tracking_settings: TrackingSettings,
    extra_tags: dict[str, str] | None = None,
) -> str | None:
    """Refit on all data, register, and move @challenger. Returns version."""
    if not tracking_settings.enabled or not tracking_settings.register_model:
        logger.debug("log_and_register_model: disabled")
        return None
    if not MLFLOW_AVAILABLE:
        logger.warning("log_and_register_model: mlflow not installed; no-op.")
        return None

    # 1. Refit (no MLflow needed)
    try:
        pipeline, feature_cols = refit_full_model(features, bt_settings)
    except Exception as e:
        logger.error("refit_full_model failed: %s", e)
        return None

    # 2. Register
    source_run_id = getattr(result, "run_id", None)
    sample_x = features.head(max(5, len(features) // 100))
    return register_model(
        pipeline=pipeline,
        feature_cols=feature_cols,
        sample_x=sample_x,
        settings=tracking_settings,
        extra_tags=extra_tags,
        source_run_id=source_run_id,
    )


__all__ = [
    "log_and_register_model",
    "refit_full_model",
    "register_model",
]
