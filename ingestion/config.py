"""Typed configuration for the ingestion layer.

All configuration flows through this module. No hardcoded paths,
no hardcoded backends. Values are read from environment variables
(with `.env` support) via pydantic-settings.

Usage:
    from ingestion.config import get_settings
    settings = get_settings()  # cached singleton
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings", "load_universe"]


class Settings(BaseSettings):
    """Application settings.

    Every field has a default that works out-of-the-box on a fresh
    checkout, so the pipeline is runnable with zero configuration.
    Override via environment variables or `.env`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Warehouse ────────────────────────────────────────────
    warehouse_backend: Literal["duckdb", "snowflake"] = "duckdb"
    duckdb_path: Path = Path("./data/warehouse.duckdb")

    # ── Market data ──────────────────────────────────────────
    price_source: str = "yfinance"
    universe_file: Path = Path("./config/universe.txt")
    benchmark_ticker: str = "SPY"
    price_history_start: date = date(2015, 1, 1)

    # ── Raw layer ────────────────────────────────────────────
    raw_data_dir: Path = Path("./data/raw")

    # ── Ingestion behavior ───────────────────────────────────
    ingest_rate_limit_seconds: float = Field(default=0.5, ge=0.0)
    ingest_max_retries: int = Field(default=5, ge=1)
    ingest_retry_min_seconds: float = Field(default=1.0, gt=0.0)
    ingest_retry_max_seconds: float = Field(default=30.0, gt=0.0)

    # ── MLflow ───────────────────────────────────────────────
    mlflow_tracking_uri: str = "./mlruns"
    mlflow_experiment_name: str = "data-backtest-pipeline"

    # ── Logging ──────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"

    # ── App ──────────────────────────────────────────────────
    streamlit_server_port: int = 8501

    # ── Derived paths ────────────────────────────────────────
    @property
    def prices_raw_dir(self) -> Path:
        """Directory holding raw price snapshots for the active source."""
        return self.raw_data_dir / "prices" / self.price_source

    @property
    def manifest_path(self) -> Path:
        """JSON manifest tracking every raw snapshot we have written."""
        return self.raw_data_dir / "prices" / "manifest.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Cached so that repeated calls inside a run share the same object
    (useful for tests that monkeypatch environment variables: call
    ``get_settings.cache_clear()`` between them).
    """
    return Settings()


def load_universe(path: Path | None = None) -> list[str]:
    """Read the universe file and return a list of ticker symbols.

    Lines starting with ``#`` and blank lines are ignored. Inline
    comments (after ``#``) are stripped. Order is preserved and
    duplicates are removed while keeping the first occurrence.

    Args:
        path: Optional override; defaults to ``settings.universe_file``.

    Returns:
        List of unique ticker symbols, uppercase.

    Raises:
        FileNotFoundError: if the universe file does not exist.
    """
    settings = get_settings()
    universe_path = path or settings.universe_file
    if not universe_path.exists():
        raise FileNotFoundError(f"Universe file not found: {universe_path}")

    seen: set[str] = set()
    tickers: list[str] = []
    for raw_line in universe_path.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        ticker = line.upper()
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers
