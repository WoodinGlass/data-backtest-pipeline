"""Macro data ingestion (FRED + ALFRED).

See ADR 0009 for the design: vintage-aware, content-addressed,
point-in-time-correct macro series.

Sub-modules:
    config.py     — typed settings for macro ingestion
    schemas.py    — Pydantic contracts
    client.py     — FRED + ALFRED HTTP client
    raw_store.py  — immutable Parquet snapshots
    pipeline.py   — orchestration
"""

__all__: list[str] = []
