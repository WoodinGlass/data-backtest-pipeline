"""Typed configuration for SEC EDGAR ingestion.

Extends the main pipeline settings with SEC-specific fields. Reads
from the same .env file; field names are distinct (SEC_*,
FUNDAMENTAL_*).
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
    "FundamentalTag",
    "SecSettings",
    "get_sec_settings",
    "load_fundamental_registry",
    "load_sec_skip_tickers",
]


class SecSettings(BaseSettings):
    """Settings for SEC EDGAR ingestion."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # SEC requires a descriptive User-Agent with contact info.
    sec_user_agent: str = Field(default="")

    fundamental_tags_file: Path = Path("./config/fundamental_tags.yml")
    fundamental_history_start: date = date(2015, 1, 1)
    fundamental_raw_data_dir: Path = Path("./data/raw/fundamentals")
    sec_skip_tickers_file: Path = Path("./config/sec_skip_tickers.yml")

    # HTTP behavior (SEC fair-use: 10 req/s max)
    sec_request_timeout_seconds: float = Field(default=30.0, gt=0.0)
    sec_rate_limit_seconds: float = Field(default=0.15, ge=0.0)
    sec_max_retries: int = Field(default=5, ge=1)
    sec_retry_min_seconds: float = Field(default=1.0, gt=0.0)
    sec_retry_max_seconds: float = Field(default=30.0, gt=0.0)

    @property
    def sec_root(self) -> Path:
        """Directory holding raw SEC snapshots."""
        return self.fundamental_raw_data_dir / "sec"

    @property
    def sec_manifest_path(self) -> Path:
        return self.fundamental_raw_data_dir / "manifest.json"


@lru_cache(maxsize=1)
def get_sec_settings() -> SecSettings:
    """Return a cached SecSettings instance."""
    return SecSettings()


class FundamentalTag:
    """One curated XBRL tag from the registry.

    Attributes:
        tag: XBRL tag name, e.g. ``Revenues``.
        namespace: ``us-gaap`` or ``dei`` or ``ifrs-full``.
        title: Short human-readable label.
        category: One of the registry categories.
    """

    __slots__ = ("category", "namespace", "tag", "title")

    def __init__(
        self,
        *,
        tag: str,
        namespace: str,
        title: str,
        category: str,
    ) -> None:
        self.tag = tag
        self.namespace = namespace
        self.title = title
        self.category = category

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"FundamentalTag(ns={self.namespace!r}, tag={self.tag!r}, category={self.category!r})"
        )


def load_fundamental_registry(
    path: Path | None = None,
) -> list[FundamentalTag]:
    """Load the curated fundamental tag registry from YAML.

    Deduplicates by (namespace, tag), keeping the first occurrence.
    Order follows category then entry order in the YAML.
    """
    settings = get_sec_settings()
    src = path or settings.fundamental_tags_file
    if not src.exists():
        raise FileNotFoundError(f"Fundamental registry not found: {src}")

    raw: Any = yaml.safe_load(src.read_text())
    if not isinstance(raw, dict) or "tags" not in raw:
        raise ValueError(f"Fundamental registry malformed: {src}")

    seen: set[tuple[str, str]] = set()
    out: list[FundamentalTag] = []
    for category, entries in raw["tags"].items():
        if not isinstance(entries, list):
            raise ValueError(f"Category {category!r} is not a list")
        for entry in entries:
            if not isinstance(entry, dict) or "tag" not in entry:
                raise ValueError(f"Entry under {category!r} malformed")
            ns = str(entry.get("namespace", "us-gaap"))
            tag = str(entry["tag"])
            key = (ns, tag)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                FundamentalTag(
                    tag=tag,
                    namespace=ns,
                    title=str(entry.get("title", tag)),
                    category=str(category),
                )
            )
    return out


def load_sec_skip_tickers(
    path: Path | None = None,
) -> set[str]:
    """Load the set of tickers to skip during SEC ingestion.

    Returns empty set if the file does not exist.
    """
    settings = get_sec_settings()
    src = path or settings.sec_skip_tickers_file
    if not src.exists():
        return set()
    raw: Any = yaml.safe_load(src.read_text())
    if not isinstance(raw, dict):
        return set()
    skip = raw.get("skip", [])
    if not isinstance(skip, list):
        return set()
    return {str(s).upper() for s in skip}
