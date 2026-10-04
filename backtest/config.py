"""Backtest settings — all M5 parameters in one place.

Design contract: docs/adr/0014-walk-forward-methodology.md

Env prefix: DBP_BT_ (case-insensitive).
Risk parameters live in risk/config.py — not here.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKTEST_VERSION: str = "v1"


class BacktestSettings(BaseSettings):
    """All walk-forward backtest parameters."""

    model_config = SettingsConfigDict(
        env_prefix="DBP_BT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Metadata ---
    version: str = Field(default=BACKTEST_VERSION)

    # --- 1. Split scheme (ADR 0014 §1) ---
    train_window_months: int = Field(default=36, gt=0)
    test_window_months: int = Field(default=3, gt=0)
    step_months: int = Field(default=3, gt=0)
    first_test_start: date | None = Field(default=None)
    last_test_end: date | None = Field(default=None)

    # --- 2. Purge + embargo (ADR 0014 §2) ---
    purge_days: int = Field(default=1, ge=0)
    embargo_days: int = Field(default=5, ge=0)

    # --- 3. Model (ADR 0014 §3) ---
    model_type: Literal["logistic"] = Field(default="logistic")
    model_C: float = Field(default=0.1, gt=0)  # noqa: N815
    model_max_iter: int = Field(default=1000, gt=0)
    model_solver: Literal["lbfgs", "liblinear", "newton-cg", "saga"] = Field(default="lbfgs")

    # --- 4. Sample weighting (ADR 0014 §4) ---
    use_time_decay: bool = Field(default=True)
    time_decay_half_life_days: int = Field(default=252, gt=0)

    # --- 5. Baselines (ADR 0014 §5) ---
    baselines: list[str] = Field(default=["b0_naive", "b1_momentum", "b2_spy", "b3_top10"])
    momentum_lookback_days: int = Field(default=20, gt=0)
    top_n_baseline: int = Field(default=10, gt=0)

    # --- 6. Metrics + aggregation (ADR 0014 §6) ---
    bootstrap_resamples: int = Field(default=1000, ge=100)
    bootstrap_ci: float = Field(default=0.95, gt=0.5, lt=1)

    # --- 7. Risk integration (ADR 0014 §7) ---
    risk_framework_version: str = Field(default="v1")

    # --- 8. Reproducibility (ADR 0014 §8) ---
    seed: int = Field(default=42, ge=0)
    output_dir: str = Field(default="data/backtest")

    # --- Inputs ---
    feature_table: str = Field(default="data/features/v1/features_daily.parquet")
    label_column: str = Field(default="next_return_positive")
    benchmark_ticker: str = Field(default="SPY")

    # --- Derived ---
    @property
    def total_gap_days(self) -> int:
        return self.purge_days + self.embargo_days

    # --- Validators ---
    @model_validator(mode="after")
    def _check_split_consistency(self) -> BacktestSettings:
        if self.step_months < self.test_window_months:
            raise ValueError(
                f"step_months ({self.step_months}) must be >= "
                f"test_window_months ({self.test_window_months})"
            )
        if self.purge_days < 1:
            raise ValueError("purge_days must be >= 1 (label horizon)")
        if (
            self.first_test_start
            and self.last_test_end
            and self.first_test_start >= self.last_test_end
        ):
            raise ValueError(
                f"first_test_start ({self.first_test_start}) must be "
                f"before last_test_end ({self.last_test_end})"
            )
        return self

    def describe(self) -> str:
        return (
            f"BacktestSettings(version={self.version}, "
            f"train={self.train_window_months}m, "
            f"test={self.test_window_months}m, "
            f"step={self.step_months}m, "
            f"purge={self.purge_days}d, embargo={self.embargo_days}d, "
            f"model={self.model_type}(C={self.model_C}), "
            f"half_life={self.time_decay_half_life_days}d, "
            f"seed={self.seed})"
        )


__all__ = ["BACKTEST_VERSION", "BacktestSettings"]
