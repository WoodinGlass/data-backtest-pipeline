# Architecture Decision Records

Every major design decision in this project is recorded as an ADR.
Format follows [MADR](https://adr.github.io/madr/) — a short
Context / Decision / Consequences structure.

## Why ADRs

A year from now, "why is `adj_close` not in the content hash?" or
"why is the drift reference the first 60 days?" is a question nobody
can answer from the code alone. The code says **what** happens; the
ADR says **why**. If the why changes, the ADR is amended in the same
PR that changes the code.

## Status legend

- **Accepted** — current design; code follows this contract.
- **Proposed** — under discussion; not yet enforced.
- **Superseded** — replaced by a later ADR (link in the ADR body).
- **Deprecated** — no longer applicable; kept for history.

## Index

| # | Title | Status | Area |
|---|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted | Meta |
| [0002](0002-warehouse-duckdb-first.md) | Warehouse: DuckDB first, Snowflake optional | Accepted | Storage |
| [0003](0003-batch-ingestion.md) | Batch ingestion (not streaming) | Accepted | Ingestion |
| [0004](0004-equities-not-sports-betting.md) | Domain: equities, not sports betting | Accepted | Scope |
| [0005](0005-universe-as-of-date.md) | Universe is defined as-of date, not as-of today | Accepted | Data quality |
| [0006](0006-content-hash-excludes-adjusted-close.md) | Content hash excludes `adj_close` | Accepted | Idempotency |
| [0007](0007-two-layer-validation.md) | Two-layer OHLCV validation | Accepted | Data quality |
| [0008](0008-three-layer-quality-defense.md) | Three-layer data quality defense | Accepted | Data quality |
| [0009](0009-macro-data-design.md) | Macro data design: FRED + ALFRED (vintage-aware) | Accepted | Data source |
| [0010](0010-fundamental-data-design.md) | Fundamental data design: SEC EDGAR XBRL (filing-date PIT) | Accepted | Data source |
| [0011](0011-memory-management-duckdb.md) | Memory management for dbt + DuckDB models | Accepted | Performance |
| [0012](0012-feature-engineering-architecture.md) | Feature engineering architecture | Accepted | Features |
| [0013](0013-risk-framework.md) | Risk framework (staking, entry, limits) | Accepted | Backtest |
| [0014](0014-walk-forward-methodology.md) | Walk-forward backtest methodology | Accepted | Backtest |
| [0015](0015-mlflow-tracking.md) | MLflow tracking and model registry | Accepted | Tracking |
| [0016](0016-prefect-orchestration.md) | Prefect orchestration | Accepted | Orchestration |
| [0017](0017-docker.md) | Docker and one-command reproducibility | Accepted | Deployment |
| [0018](0018-ci-contract.md) | CI contract and merge blocking | Accepted | CI/CD |
| [0019](0019-monitoring-dashboard.md) | Monitoring and Streamlit dashboard | Accepted | Monitoring |
| [0020](0020-deployment-and-summary.md) | Deployment target and research summary | Accepted | Deployment |

## Grouping by theme

### Data foundation (M1-M3)
- **0002** (DuckDB), **0003** (batch), **0005** (universe PIT),
  **0006** (hash), **0007** (validation), **0008** (three layers),
  **0009** (macro), **0010** (fundamental), **0011** (memory).

### Modeling (M4-M5)
- **0004** (equities), **0012** (features), **0013** (risk),
  **0014** (walk-forward).

### Operational (M6-M10)
- **0015** (MLflow), **0016** (Prefect), **0017** (Docker),
  **0018** (CI), **0019** (monitoring).

### Meta
- **0001** (why ADRs).

## Reading order for new contributors

1. **0001** — why this format exists.
2. **0002, 0003** — where the data lives and how it arrives.
3. **0005, 0006, 0007, 0008** — the anti-leakage / idempotency story.
4. **0009, 0010** — the two non-price data sources, both PIT.
5. **0012, 0013, 0014** — features, risk, backtest: the model pipeline.
6. **0015, 0016, 0017, 0018, 0019** — tracking, orchestration, deploy,
   CI, monitoring: the operational shell.

## Adding a new ADR

1. Copy the structure from a recent ADR (e.g. 0018 or 0019).
2. Pick the next number. Do not renumber existing ADRs.
3. Fill in **Context** (why are we deciding this now?), **Decision**
   (numbered list of what we chose), and **Consequences**
   (positive / negative / neutral).
4. Add an entry to the index table above.
5. Link the ADR from the relevant section of the README and from
   the code's module docstring.

**Amending an ADR** (rather than creating a new one) is appropriate
when:
- The core decision is unchanged, but a detail has moved.
- A numeric threshold (e.g. coverage floor) has been raised.
- A "future work" section has been resolved.

Amendments go at the bottom under a `## Amendments` section, dated
and attributed. See ADR 0018 §9 for the maintenance policy.

**Superseding an ADR** (creating a new one that replaces it) is
appropriate when the core decision has changed. Mark the old one
`Status: Superseded by NNNN` and cross-link both.

## Naming convention

`NNNN-kebab-case-title.md`, e.g. `0014-walk-forward-methodology.md`.
Four-digit zero-padded numbers. Lowercase words separated by hyphens.
The filename is stable once committed; only the title line inside
the file may be polished later.
