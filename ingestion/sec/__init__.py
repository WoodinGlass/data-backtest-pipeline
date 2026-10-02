"""SEC EDGAR fundamental ingestion (XBRL companyfacts).

See ADR 0010 for the design: filing-date PIT, content-addressed
immutable raw layer, one Parquet per ticker.

Sub-modules:
    config.py     — typed settings for SEC ingestion
    schemas.py    — Pydantic contracts
    client.py     — SEC EDGAR HTTP client (companyfacts API)
    raw_store.py  — immutable Parquet snapshots
    pipeline.py   — orchestration
    cli.py        — dbp-ingest-sec entry point
"""

__all__: list[str] = []
