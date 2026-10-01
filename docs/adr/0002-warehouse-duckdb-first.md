# 2. Warehouse: DuckDB first, Snowflake optional

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

We need an analytical warehouse for dbt models. The dataset is small
(daily bars for ~30 tickers, ~10 years), and we want the repo to be
reproducible on a laptop.

## Decision

Use **DuckDB** as the default warehouse. Support **Snowflake** as an
optional backend behind `WAREHOUSE_BACKEND`, without changing any model code.

## Consequences

- Local development needs zero external services.
- dbt-duckdb reads/writes a single file — trivial to reset and to gitignore.
- Snowflake path exists but is not exercised in CI; a dedicated integration
  job would be required to keep it honest.
