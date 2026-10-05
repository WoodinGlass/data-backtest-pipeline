"""Monitoring settings. ADR 0019.

Env prefix: DBP_MON_ (case-insensitive).

All thresholds and paths live here. Downstream modules
(freshness, drift, performance) read from a single
MonitoringSettings instance — nothing is hardcoded.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MONITORING_VERSION: str = "v1"


class MartThreshold(BaseModel):
    """WARN / FAIL thresholds for a single mart, in calendar days."""

    warn_days: int = Field(gt=0)
    fail_days: int = Field(gt=0)

    @model_validator(mode="after")
    def _warn_before_fail(self) -> MartThreshold:
        if self.warn_days >= self.fail_days:
            raise ValueError(f"warn_days ({self.warn_days}) must be < fail_days ({self.fail_days})")
        return self


def _default_marts() -> dict[str, MartThreshold]:
    """Freshness thresholds for the default mart set.

    Prices and returns update daily; macro is monthly; fundamentals
    are quarterly. One number cannot fit all three.
    """
    return {
        "fct_prices_daily": MartThreshold(warn_days=3, fail_days=7),
        "fct_returns_daily": MartThreshold(warn_days=3, fail_days=7),
        "fct_macro_daily": MartThreshold(warn_days=45, fail_days=90),
        "fct_fundamentals_daily": MartThreshold(warn_days=120, fail_days=180),
    }


class MonitoringSettings(BaseSettings):
    """All monitoring parameters."""

    model_config = SettingsConfigDict(
        env_prefix="DBP_MON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Metadata ---
    version: str = Field(default=MONITORING_VERSION)

    # --- Reference date ---
    # If None, use the wall clock (UTC). Tests override to a fixed date.
    reference_date: date | None = Field(
        default=None,
        description="Override 'today' for freshness. None -> wall clock.",
    )

    # --- Freshness (ADR 0019 section 3) ---
    marts: dict[str, MartThreshold] = Field(
        default_factory=_default_marts,
        description="Per-mart WARN/FAIL thresholds, calendar days.",
    )

    # --- Drift (ADR 0019 section 4, section 5) ---
    drift_reference_days: int = Field(
        default=60,
        gt=0,
        description="Size of the reference window, in trading days.",
    )
    drift_current_days: int = Field(
        default=60,
        gt=0,
        description="Size of the current window, in trading days.",
    )
    drift_psi_warn: float = Field(
        default=0.10,
        gt=0,
        description="PSI at or above this is WARN.",
    )
    drift_psi_fail: float = Field(
        default=0.25,
        gt=0,
        description="PSI at or above this is FAIL.",
    )
    drift_ks_pvalue_warn: float = Field(
        default=0.05,
        gt=0,
        lt=1,
        description="KS p-value at or below this is WARN.",
    )
    drift_ks_pvalue_fail: float = Field(
        default=0.01,
        gt=0,
        lt=1,
        description="KS p-value at or below this is FAIL.",
    )
    drift_exclude_columns: list[str] = Field(
        default_factory=lambda: [
            "ticker",
            "trade_date",
            "next_return",
            "next_return_positive",
        ],
        description="Columns excluded from drift computation.",
    )

    # --- Performance (ADR 0019 section 6) ---
    perf_window_folds: int = Field(
        default=4,
        gt=0,
        description="Rolling window size over folds, in number of folds.",
    )

    # --- Paths ---
    warehouse_path: str = Field(default="data/warehouse.duckdb")
    warehouse_schema: str = Field(
        default="marts",
        description="DuckDB schema holding the marts. Empty = no prefix.",
    )
    features_path: str = Field(
        default="data/features/v1/features_daily.parquet",
    )
    backtest_dir: str = Field(default="data/backtest")
    output_path: str = Field(default="reports/monitoring.json")

    # --- Validators ---
    @model_validator(mode="after")
    def _check_cross_field(self) -> MonitoringSettings:
        if self.drift_psi_warn >= self.drift_psi_fail:
            raise ValueError(
                f"drift_psi_warn ({self.drift_psi_warn}) must be < "
                f"drift_psi_fail ({self.drift_psi_fail})"
            )
        if self.drift_ks_pvalue_warn <= self.drift_ks_pvalue_fail:
            raise ValueError(
                f"drift_ks_pvalue_warn ({self.drift_ks_pvalue_warn}) "
                f"must be > drift_ks_pvalue_fail "
                f"({self.drift_ks_pvalue_fail})"
            )
        return self

    def describe(self) -> str:
        return (
            f"MonitoringSettings(version={self.version}, "
            f"marts={len(self.marts)}, "
            f"drift_window={self.drift_reference_days}/{self.drift_current_days}d, "
            f"psi_warn/fail={self.drift_psi_warn}/{self.drift_psi_fail}, "
            f"perf_window={self.perf_window_folds} folds)"
        )


__all__ = ["MONITORING_VERSION", "MartThreshold", "MonitoringSettings"]
