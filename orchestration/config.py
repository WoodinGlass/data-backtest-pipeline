"""Orchestration settings. ADR 0016.

Env prefix: DBP_ORCH_ (case-insensitive).

This module contains no pipeline logic. It only describes how
flows are scheduled, retried, and alerted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ORCHESTRATION_VERSION: str = "v1"


class OrchestrationSettings(BaseSettings):
    """All Prefect orchestration parameters."""

    model_config = SettingsConfigDict(
        env_prefix="DBP_ORCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Metadata ---
    version: str = Field(
        default=ORCHESTRATION_VERSION,
        description="Orchestration layer version.",
    )

    # --- 2. Deployment mode (ADR 0016 §2) ---
    deployment_mode: Literal["ephemeral", "served"] = Field(
        default="ephemeral",
        description="ephemeral = in-process; served = flow.serve().",
    )
    prefect_api_url: str | None = Field(
        default=None,
        description="Prefect API URL. None in ephemeral mode.",
    )

    # --- 3. Storage (ADR 0016 §3) ---
    prefect_home: str | None = Field(
        default=None,
        description="Override PREFECT_HOME. None -> Prefect default.",
    )

    # --- 6. Schedules (ADR 0016 §6, cron UTC) ---
    daily_cron: str = Field(
        default="0 22 * * 1-5",
        description="Cron for daily_refresh (UTC).",
    )
    weekly_cron: str = Field(
        default="0 23 * * 0",
        description="Cron for weekly_refresh (UTC).",
    )

    # --- 7. Retry policy (ADR 0016 §7) ---
    retry_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Max attempts per task (including the first).",
    )
    retry_delay_seconds: list[int] = Field(
        default=[10, 60, 300],
        description="Delay before each retry, in seconds.",
    )

    # --- 8. Alerts (ADR 0016 §8) ---
    alert_webhook_url: str | None = Field(
        default=None,
        description="POST target on terminal flow failure. Best-effort.",
    )
    alert_webhook_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        le=60.0,
        description="HTTP timeout for the alert webhook.",
    )
    alert_on_retry: bool = Field(
        default=False,
        description="If True, also fire the webhook on each retry.",
    )

    # --- Task subprocess behavior ---
    subprocess_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Log level passed to subprocess CLI calls.",
    )
    subprocess_capture_output: bool = Field(
        default=True,
        description="Capture stdout/stderr (True) or stream (False).",
    )

    # --- Repo paths ---
    repo_root: str | None = Field(
        default=None,
        description="Override repo root discovery. None -> auto.",
    )

    # --- Validators ---
    @field_validator("alert_webhook_url")
    @classmethod
    def _check_webhook_scheme(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("alert_webhook_url must be http(s)://; got " + repr(v))
        return v

    @field_validator("retry_delay_seconds")
    @classmethod
    def _check_delay_length(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("retry_delay_seconds must not be empty")
        if any(d < 0 for d in v):
            raise ValueError("retry_delay_seconds must be non-negative")
        return v

    def describe(self) -> str:
        return (
            f"OrchestrationSettings(version={self.version}, "
            f"mode={self.deployment_mode}, "
            f"retries={self.retry_attempts}, "
            f"webhook={'on' if self.alert_webhook_url else 'off'}, "
            f"daily_cron={self.daily_cron!r}, "
            f"weekly_cron={self.weekly_cron!r})"
        )


__all__ = ["ORCHESTRATION_VERSION", "OrchestrationSettings"]
