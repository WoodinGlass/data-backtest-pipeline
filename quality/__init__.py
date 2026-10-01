"""Data quality gate for the pipeline.

Validates the raw Parquet layer and the dbt-built warehouse against
declarative Pandera schemas. A violation is a hard failure: the gate
exits non-zero, blocking downstream work (features, backtest).

Modules:
    schemas.py  — Pandera DataFrameModel per table, one per layer
    gate.py     — runner: load -> validate -> report
    cli.py      — `python -m quality.cli [--layer ...] [--json]`

The gate complements (does not replace) the existing checks:

    ingestion/validation.py    row-level, in the ingestion hot path
    dbt/models/**/*.yml        SQL-level, in the warehouse
    quality/                   DataFrame-level, at layer boundaries

Layering rationale: ADR 0007 (two-layer validation) and ADR 0008 (M3).
"""

__all__: list[str] = []
