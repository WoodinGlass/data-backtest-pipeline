# 11. Memory management for dbt + DuckDB models

- **Status:** Accepted
- **Date:** 2025-10-03

## Context

Two dbt models in this project are large:

| Model | Output rows | Join size | Peak memory |
|---|---|---|---|
| `int_macro_daily` | ~394K | 2,954 × 140 × vintages | ~3 GB |
| `int_fundamentals_pit` | 12.5M | 2,954 × 31 × 137 | **>5.5 GB** |

Building the fundamentals model monolithically caused an Out Of
Memory error in Colab (12 GB RAM):

```
Out of Memory Error: failed to pin block of size 256.0 KiB
(5.5 GiB/5.5 GiB used)
```

The first fix attempt — `--threads 1` + `SET memory_limit='6GB'` +
`SET temp_directory='/tmp/duckdb_spill'` — still OOM'd after 40
minutes. The second attempt — replacing the window function
(`ROW_NUMBER() OVER ... QUALIFY`) with `GROUP BY arg_max` — also
OOM'd. The final design (below) completes in ~70 seconds.

The difference between "40 minutes then killed" and "70 seconds,
success" is worth documenting because it is not intuitive.

## Decision

Four techniques, applied together:

### 1. Split by partition key (year)

Instead of one query over the full time range, emit a UNION of
per-year subqueries:

```sql
{% for yr in [2015, 2016, ..., 2026] %}
select ... from trade_dates where year = {{ yr }}
cross join ...
group by ...
{% if not loop.last %}union all{% endif %}
{% endfor %}
```

Each subquery processes ~1.06M pairs. Peak memory is roughly 1/12
of the monolithic version. Total I/O is the same; the win is that
DuckDB never needs to hold the entire intermediate result at once.

**This is the single most important fix.** The other three
techniques alone did not save the run.

### 2. Prefer `GROUP BY arg_max` over window functions

Window functions (`ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)`)
require DuckDB to materialize and sort the entire partition set in
memory. Aggregates (`arg_max`, `min`, `max`, `sum`) can spill to
disk via `temp_directory`.

Replace:

```sql
select *, row_number() over (
    partition by k1, k2 order by ts desc
) as rn from t
qualify rn = 1
```

with:

```sql
select k1, k2,
       arg_max(value, ts) as value,
       max(ts) as ts
from t group by k1, k2
```

`arg_max(v, k)` returns the value of `v` at the row where `k` is
largest — exactly what "latest row per group" means.

### 3. Set explicit memory + spill directory

Per-model `pre_hook`:

```sql
{{ config(
    materialized='table',
    pre_hook=[
        "SET memory_limit='6GB'",
        "SET temp_directory='/tmp/duckdb_spill'",
        "SET preserve_insertion_order=false",
    ]
) }}
```

- `memory_limit='6GB'` leaves ~6 GB for Python, dbt, and OS.
- `temp_directory` enables spilling. Without it, OOM is immediate.
- `preserve_insertion_order=false` frees buffer used to remember
  input row order. Safe when the final result is consumed by SQL.

### 4. Pre-aggregate small before joining big

Before the big join, collapse the facts to one row per
`(ticker, namespace, tag, filed)` — a ~60K-row CTE. The huge join
then runs between 1.06M pairs and 60K rows, not between 1.06M pairs
and 904K rows.

## Consequences

**Positive**

- `int_fundamentals_pit` builds in ~70s on a 12 GB Colab instance.
- Same techniques apply to future large models (M4 features will
  produce wide tables of similar size).
- The pattern is portable: partition-split + aggregate-not-window
  + explicit memory config works on any DuckDB deployment.

**Negative / trade-offs**

- Year-split couples the model to the time range. Adding a new year
  to the data requires adding it to the `years` list in the Jinja.
  This is acceptable: years are known and the list is short.
- `preserve_insertion_order=false` changes output row order. Any
  consumer that assumes order must add an explicit `ORDER BY`. None
  do in this project.
- Debugging is slightly harder: `dbt run --select model` no longer
  shows a single query in the log; it shows the UNION.

**Explicitly rejected**

- *Increase Colab RAM.* The free tier caps at ~12 GB. Throwing
  hardware at the problem hides the underlying inefficiency and
  does not work in CI.
- *Materialize intermediate steps as separate models.* Would work
  but adds 3+ models with no domain meaning. The year-split is
  a pure execution concern; it belongs in one model file.
- *Switch to a real warehouse (Snowflake/BigQuery).* Overkill for
  a portfolio project and conflicts with the DuckDB-first design
  in ADR 0002.

## Pattern for future large models

When a dbt model's output is expected to exceed ~1M rows:

1. **Can it be partitioned?** If yes, add a `{% for ... %}` loop.
   This is the highest-leverage move.
2. **Any window function?** Replace with `arg_max` / `max_by` /
   `first_value` aggregate if possible.
3. **Set memory and spill** in the model's `pre_hook`.
4. **Pre-aggregate the small side** of the join before the big one.

## Related

- ADR 0002 — DuckDB-first warehouse.
- ADR 0010 — SEC fundamental data design (this ADR explains how
  `int_fundamentals_pit` is built).
