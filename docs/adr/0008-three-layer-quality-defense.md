# 8. Three-layer data quality defense

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

By the end of M2 we had three overlapping mechanisms for detecting
bad data, without a clear rationale for which mechanism lives where:

1. `ingestion/validation.py` — vectorized checks before Parquet writes.
2. `dbt/models/**/*.yml` — SQL-level tests in the warehouse.
3. (New in M3) A dedicated data quality gate.

This raised a legitimate question: *is this duplication, or defense in
depth?* If the answer is "defense in depth", we owe a clear statement
of **which check belongs where**, and why.

Three concrete requirements shaped the answer:

- **Fast fail during ingest.** Bad upstream data must not land in the
  immutable raw layer at all. Cost matters: ingestion runs per ticker.
- **Declarative contracts at layer boundaries.** Reviewers and future
  contributors should be able to read a schema, not re-derive it from
  code. This is what Pandera gives us.
- **SQL-native checks in the warehouse.** Some invariants are cheap to
  express (and to iterate on) as SQL against a view or table.

One approach does not satisfy all three. So we accept three layers,
with a clear division of labour and a rule that prevents drift.

## Decision

Three layers, each with a distinct scope and cost profile:

| Layer | Tool | Where | Fails | Cost |
|---|---|---|---|---|
| Row/frame | `ingestion/validation.py` | Ingestion hot path | Hard, per ticker | <1% of runtime |
| Contract | `quality/schemas.py` + `gate.py` | Layer boundaries | Hard, per layer | ~2s for 32 tickers |
| SQL | `dbt/models/**/*.yml` + `dbt/tests/` | Warehouse build | Hard, per test | ~0.1–1s per test |

**Division of labour:**

- If a rule prevents **corrupt bytes from reaching the raw layer**,
  it lives in `ingestion/validation.py`. It runs first, cheaply, and
  is the only line of defense *before* persistence.
- If a rule expresses a **contract between layers** (raw → staging →
  marts), it lives in `quality/schemas.py`. This is where Pandera's
  declarative model earns its keep: the schema *is* the documentation.
- If a rule is **naturally expressed in SQL** against a built model
  (e.g. cross-table referential integrity, or a summary statistic over
  a wide table), it lives in dbt tests.

**Rule to prevent drift:** when a rule is added to one layer, the PR
description must state why it does not belong in either of the other
two. If the answer is "it could live in any of them", the rule goes in
the earliest layer that can express it.

## Consequences

**Positive**

- Fast fail in ingest: the raw layer is never corrupt.
- Declarative schemas are readable by non-authors, and Pandera errors
  are specific (row, column, expected, actual).
- dbt tests keep the warehouse verifiable from SQL, which is what
  most data engineers reach for first.

**Negative / trade-offs**

- Three places to keep in sync. Mitigated by the drift rule above and
  by the fact that each rule lives in exactly one place.
- Developers must know which layer to touch. Mitigated by the table in
  `docs/runbook.md`.

**Explicitly rejected**

- *Single mechanism.* Would sacrifice either speed (Pandera on every
  ingest) or clarity (vectorized checks everywhere) or SQL-ergonomics
  (no native SQL checks).
- *Move everything to Pandera.* Loses fast-fail in ingest, and Pandera
  cannot replace dbt tests for cross-table invariants.

## Related

- ADR 0006 — content hash excludes adj_close (a rule that lives in
  ingestion/raw_store.py, not in the gate, because it is about
  content identity, not data quality).
- ADR 0007 — two-layer validation (Pydantic for edges, vectorized for
  frames). This ADR extends the same reasoning to the *contract* layer.
