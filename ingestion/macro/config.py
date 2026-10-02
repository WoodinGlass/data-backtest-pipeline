"""Typed configuration for macro ingestion.

Extends the main pipeline Settings with FRED-specific fields.
All values default to sensible values so that a fresh checkout can
run `make ingest-macro` with only `FRED_API_KEY` set.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "MacroSeries",
    "MacroSettings",
    "get_macro_settings",
    "load_macro_registry",
]


class MacroSettings(BaseSettings):
    """Settings specific to macro ingestion.

    Read from the same .env as the main Settings; the field names are
    distinct (FRED_API_KEY, MACRO_*).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    fred_api_key: str = Field(default="", description="FRED API key")
    macro_series_file: Path = Path("./config/macro_series.yml")
    macro_history_start: date = date(2015, 1, 1)
    macro_raw_data_dir: Path = Path("./data/raw/macro")

    # HTTP behavior
    macro_request_timeout_seconds: float = Field(default=20.0, gt=0.0)
    macro_rate_limit_seconds: float = Field(default=0.6, ge=0.0)
    macro_max_retries: int = Field(default=5, ge=1)
    macro_retry_min_seconds: float = Field(default=1.0, gt=0.0)
    macro_retry_max_seconds: float = Field(default=30.0, gt=0.0)

    @property
    def fred_root(self) -> Path:
        """Directory holding raw FRED snapshots."""
        return self.macro_raw_data_dir / "fred"

    @property
    def macro_manifest_path(self) -> Path:
        return self.macro_raw_data_dir / "manifest.json"


@lru_cache(maxsize=1)
def get_macro_settings() -> MacroSettings:
    """Return a cached MacroSettings instance."""
    return MacroSettings()


class MacroSeries:
    """One curated FRED series.

    Attributes:
        series_id: FRED series id, e.g. ``FEDFUNDS``.
        title: Short human-readable label from the registry.
        category: One of the ten curated categories.
    """

    __slots__ = ("category", "series_id", "title")

    def __init__(self, *, series_id: str, title: str, category: str) -> None:
        self.series_id = series_id
        self.title = title
        self.category = category

    def __repr__(self) -> str:  # pragma: no cover — debug convenience
        return f"MacroSeries(id={self.series_id!r}, category={self.category!r})"


def load_macro_registry(
    path: Path | None = None,
) -> list[MacroSeries]:
    """Load the curated macro series registry from YAML.

    Args:
        path: Optional override; defaults to
            ``get_macro_settings().macro_series_file``.

    Returns:
        Flat list of :class:`MacroSeries`, in registry order.

    Raises:
        FileNotFoundError: if the YAML file does not exist.
        ValueError: if the YAML structure is not as expected.
    """
    settings = get_macro_settings()
    src = path or settings.macro_series_file
    if not src.exists():
        raise FileNotFoundError(f"Macro registry not found: {src}")

    raw: Any = yaml.safe_load(src.read_text())
    if not isinstance(raw, dict) or "series" not in raw:
        raise ValueError(f"Macro registry malformed: {src}")

    out: list[MacroSeries] = []
    for category, entries in raw["series"].items():
        if not isinstance(entries, list):
            raise ValueError(f"Category {category!r} is not a list in {src}")
        for entry in entries:
            if not isinstance(entry, dict) or "id" not in entry:
                raise ValueError(f"Entry under {category!r} missing 'id': {entry!r}")
            out.append(
                MacroSeries(
                    series_id=str(entry["id"]),
                    title=str(entry.get("title", entry["id"])),
                    category=str(category),
                )
            )
    return out
