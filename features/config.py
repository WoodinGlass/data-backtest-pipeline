"""Typed configuration for the feature layer.

Two things this module owns:

- **``FEATURE_VERSION``**: a manual version string. Bumping it creates
  a new output directory under ``data/features/``; old versions
  remain. This mirrors the content-addressing discipline of the raw
  layer (ADR 0006, 0009, 0010) and makes feature change explicit.

- **Paths and window parameters**: rolling window lengths, benchmark
  ticker, output directory. All read from the same environment as the
  rest of the pipeline, with defaults that work out of the box.

Why Python, not dbt
-------------------
Features are computed in Python (see ADR 0012). Rolling-window
features (RSI, momentum, volatility) and cross-sectional ranks are
awkward in SQL. Anti-leakage tests need to shuffle the input
DataFrame and compare outputs; that is a Python test, not a dbt
test. Feature builders are pure functions: input DataFrame in,
output DataFrame out, no IO. A thin dbt model
(``marts.fct_features_daily``) reads the resulting Parquet back into
the warehouse so the backtest (M5) can query it like any other table.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "FEATURE_VERSION",
    "FeatureSettings",
    "get_feature_settings",
]


# Version string. Bump when the feature set changes in a way that
# affects downstream models. Old versions stay on disk; the backtest
# (M5) reads a specific version by name.
FEATURE_VERSION = "v1"


class FeatureSettings(BaseSettings):
    """Settings for the feature layer.

    Reads the same ``.env`` as the rest of the pipeline; field names
    are distinct (``FEATURE_*``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Output location for feature Parquet.
    feature_data_dir: Path = Path("./data/features")
    feature_version: str = FEATURE_VERSION

    # Input locations (marts in the warehouse).
    warehouse_path: Path = Path("./data/warehouse.duckdb")
    benchmark_ticker: str = "SPY"

    # Window lengths (in trading days). Centralised so a reviewer can
    # see every horizon the feature layer uses in one place.
    #
    # Constraints: only `ge=2` is enforced (a rolling window of 1 is
    # meaningless). Defaults match typical equity practice; tests
    # override with small windows for fast synthetic data.
    window_short: int = Field(default=5, ge=2)
    window_medium: int = Field(default=20, ge=2)
    window_long: int = Field(default=60, ge=2)
    window_rsi: int = Field(default=14, ge=2)

    # Annualisation factor for volatility (252 trading days per year).
    trading_days_per_year: int = Field(default=252, ge=200, le=260)

    @property
    def version_dir(self) -> Path:
        """Directory for the current feature version."""
        return self.feature_data_dir / self.feature_version

    @property
    def features_path(self) -> Path:
        """Path to the wide feature Parquet for the current version."""
        return self.version_dir / "features_daily.parquet"

    @property
    def manifest_path(self) -> Path:
        """JSON manifest for the feature layer (one file for all versions)."""
        return self.feature_data_dir / "manifest.json"


@lru_cache(maxsize=1)
def get_feature_settings() -> FeatureSettings:
    """Return a cached FeatureSettings instance."""
    return FeatureSettings()
