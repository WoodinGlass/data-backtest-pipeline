"""Tracking settings — all M6 parameters in one place.

Design contract: docs/adr/0015-mlflow-tracking.md

Env prefix: DBP_TRACK_ (case-insensitive).

This module does NOT define backtest or risk parameters. It only
describes where runs are logged and how they are named.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TRACKING_VERSION: str = "v1"


class TrackingSettings(BaseSettings):
    """All MLflow tracking parameters."""

    model_config = SettingsConfigDict(
        env_prefix="DBP_TRACK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Metadata ---
    version: str = Field(
        default=TRACKING_VERSION,
        description="Tracking layer version. Bump on breaking changes.",
    )

    # --- 1. Backend (ADR 0015 §1) ---
    tracking_uri: str = Field(
        default="sqlite:///mlruns/mlflow.db",
        description=(
            "MLflow tracking URI. SQLite is the default because the "
            "Model Registry requires a database backend. Use an "
            "absolute path (sqlite:////abs/path.db) if running from a "
            "different cwd."
        ),
    )
    artifact_root: str | None = Field(
        default=None,
        description=(
            "Base directory for run artifacts. None -> MLflow default (next to the tracking store)."
        ),
    )

    # --- 2. Experiment (ADR 0015 §2) ---
    experiment_name: str = Field(
        default="daily-direction",
        description="Single experiment for the whole project.",
    )

    # --- 3. Opt-in / opt-out (ADR 0015 §8) ---
    enabled: bool = Field(
        default=False,
        description=(
            "Master switch. Scripts should pass enabled=True only when "
            "the user asked for MLflow (e.g. --mlflow flag)."
        ),
    )

    # --- 5. Metrics (ADR 0015 §5) ---
    log_fold_metrics: bool = Field(
        default=True,
        description="Log per-fold Sharpe/AUC as fold/<id>/<metric>.",
    )
    max_fold_metrics: int = Field(
        default=50,
        gt=0,
        description=(
            "Hard cap on number of per-fold metric entries. Extra "
            "folds are skipped. Guards against a runaway run with "
            "hundreds of folds bloating MLflow."
        ),
    )

    # --- 6. Artifacts (ADR 0015 §6) ---
    log_git_diff: bool = Field(
        default=True,
        description="Log `git diff HEAD` as artifact if not clean.",
    )
    git_diff_max_bytes: int = Field(
        default=100_000,
        gt=0,
        description="Truncate git diff at this size.",
    )
    log_env: bool = Field(
        default=True,
        description="Log `pip freeze` output as artifact.",
    )
    env_max_lines: int = Field(
        default=50,
        gt=0,
        description="Truncate pip freeze to top N lines.",
    )

    # --- 7. Model Registry (ADR 0015 §7) ---
    register_model: bool = Field(
        default=True,
        description="Refit on all data and register the model after a run.",
    )
    model_name: str = Field(
        default="daily-direction-model",
        description="Registered model name in the MLflow Model Registry.",
    )
    challenger_alias: str = Field(
        default="challenger",
        description="Alias auto-moved to the newest version.",
    )
    champion_alias: str = Field(
        default="champion",
        description=("Alias reserved for manual promotion; never moved by code."),
    )

    # --- Run metadata ---
    tags_extra: dict[str, str] = Field(
        default_factory=dict,
        description="User-defined tags merged into every run.",
    )

    # --- Validators ---
    @field_validator("tracking_uri")
    @classmethod
    def _check_uri_scheme(cls, v: str) -> str:
        allowed_prefixes = (
            "sqlite://",
            "file://",
            "http://",
            "https://",
            "postgresql://",
            "postgres://",
            "mysql://",
            "databricks",
            "databricks://",
        )
        if not v.startswith(allowed_prefixes):
            raise ValueError(f"tracking_uri must start with one of {allowed_prefixes}; got {v!r}.")
        return v

    def describe(self) -> str:
        return (
            f"TrackingSettings(version={self.version}, "
            f"enabled={self.enabled}, "
            f"uri={self.tracking_uri}, "
            f"experiment={self.experiment_name!r}, "
            f"register={self.register_model}, "
            f"model_name={self.model_name!r})"
        )


__all__ = ["TRACKING_VERSION", "TrackingSettings"]
