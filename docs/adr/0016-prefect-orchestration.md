# ADR 0016: Prefect Orchestration

- **Status:** Accepted
- **Date:** 2026-10-04
- **Related:** ADR 0002 (DuckDB), ADR 0003 (batch ingestion),
  ADR 0008 (three-layer quality), ADR 0013 (risk), ADR 0014 (walk-forward),
  ADR 0015 (MLflow tracking)

## Context

By the end of M6 the project has a complete pipeline:

- **M1/M3.5/M3.7** ingest (prices, macro, fundamentals) — idempotent.
- **M2/M3.6/M3.8** warehouse via dbt — deterministic.
- **M3/M3.9** quality gate — hard-fail on violations.
- **M4** point-in-time features — versioned.
- **M4.5** risk framework — pure, locked.
- **M5** walk-forward backtest — reproducible.
- **M6** MLflow tracking — opt-in, best-effort.

Every piece already runs. What is missing is **scheduling and
failure handling**: today a human runs `make ingest`, then
`make dbt-build`, then `make features`, then `make backtest`,
watching for errors. If a step silently fails at 03:00 UTC, the
next human to notice is the maintainer at breakfast.

M7 introduces an orchestration layer so that the pipeline runs
on a schedule, retries transient failures, and alerts on
terminal failures. It must **not** change pipeline semantics.

## Decision

We adopt Prefect 3.x with ten locked decisions. All are
implemented under `orchestration/`.

### 1. Version: Prefect 3.x

Prefect 3 (released late 2024) is stable, has a cleaner
task/flow API (`@flow`, `@task`), first-class async, and a
unified `flow.serve()` path for both ephemeral and served
deployments. `pyproject.toml` pins `prefect>=3.0,<4.0`.

Prefect 2 is not supported by this project. Any future
migration to Prefect 4 would require an ADR amendment.

### 2. Deployment mode: local ephemeral by default

Two modes coexist:

| Mode | When | Prefect API |
|------|------|-------------|
| **Ephemeral** | Colab, CI, ad-hoc | None (in-process) |
| **Served** | Docker compose, long-running | Local Prefect server + SQLite |

Ephemeral is the default. `python -m orchestration.cli run <flow>`
runs a flow in the current process without a server. This keeps
Colab workflows simple and makes the layer testable without
spinning up infrastructure.

Served mode (`flow.serve()`) is only used in Docker and is
documented in ADR 0017 (M8).

### 3. Storage: SQLite at `~/.prefect/prefect.db`

Same trade-off as ADR 0002 (DuckDB) and ADR 0015 (MLflow):
single-file, zero-server, portable. A shared Postgres backend
is future work and would require an ADR if multiple
contributors start scheduling in parallel.

### 4. Flow granularity: one flow per pipeline stage

Each stage already has a CLI entry point. The orchestration
layer wraps each CLI as a Prefect flow. No stage logic is
reimplemented.

| Flow | Wraps | Idempotent? |
|------|-------|-------------|
| `ingest_prices` | `python -m ingestion.cli` | Yes (M1 content-addressed) |
| `ingest_macro` | `python -m ingestion.macro.cli` | Yes (M3.5) |
| `ingest_sec` | `python -m ingestion.sec.cli` | Yes (M3.7) |
| `dbt_build` | `dbt build` with var selector | Yes (deterministic) |
| `quality_gate` | `python -m quality.cli` | Yes (read-only) |
| `build_features` | `python -m features.cli` | Yes (versioned) |
| `run_backtest` | `scripts/run_backtest.py` | Yes (M5 seed) |
| `track_run` | `tracking.log_backtest_run` | Yes (M6) |

### 5. Composite flows: daily, weekly, full

Three composite flows orchestrate the stage flows. Each is a
simple linear sequence; no branching, no parallelism, no
conditional logic beyond the retry policy (decision 7).

| Composite | Sequence |
|-----------|----------|
| `daily_refresh` | prices -> dbt(prices) -> quality -> features |
| `weekly_refresh` | macro + sec -> dbt(all) -> quality -> features -> backtest -> track |
| `full_refresh` | macro + sec + prices -> dbt(all) -> quality -> features -> backtest -> track |

Rationale for splitting daily vs weekly:

- Prices update daily; macro and fundamentals do not.
- The backtest is only meaningful after the weekly rebuild —
  running it daily would just produce the same numbers.
- Daily keeps the warehouse fresh for external consumers;
  weekly produces a new tracked run for M6 and the model
  registry.

### 6. Schedule (cron, UTC)

| Flow | Cron | Rationale |
|------|------|-----------|
| `daily_refresh` | `0 22 * * 1-5` | After US market close and after yfinance has updated |
| `weekly_refresh` | `0 23 * * 0` | Sunday night UTC, no market impact |
| `full_refresh` | none (manual) | Cold start and disaster recovery |

Schedules are registered by `orchestration/deployments.py`
via `flow.serve()`. They are **not** active when running
ephemerally — ephemeral callers invoke the flow directly.

### 7. Retry policy: 3 attempts, backoff [10, 60, 300] s

All tasks use the same default retry policy. Rationale:
the two dominant transient failures are upstream API
flakiness (yfinance, FRED, SEC) and local DuckDB lock
contention. Both usually clear within 5 minutes.

```python
retries = (3,)
retry_delay_seconds = ([10, 60, 300],)
```

Non-retryable failures (schema violation, dbt test failure,
quality gate hard fail) still retry three times because the
attempts are cheap and harmless. Terminal failure after the
third attempt triggers an alert (decision 8).

### 8. Alerts: structured log + optional webhook

Two layers, always applied in this order:

1. **Structured log** (always). Every task emits a JSON record
   on success and failure via the existing logging config.
   Fields: `flow_name`, `flow_run_id`, `task_name`,
   `attempt`, `error_type`, `error_msg`, `duration_ms`.
2. **Webhook** (opt-in). If `DBP_ORCH_ALERT_WEBHOOK_URL` is set,
   a JSON payload is POSTed on terminal flow failure. The
   payload is compatible with Slack incoming webhooks and
   generic endpoints. The post is **best-effort**: a failed
   webhook never masks the original failure or changes the
   flow's exit status.

No email. No pager. No SMS. This is a single-developer project.

### 9. Testing strategy

Three tiers, matching the project's existing conventions:

- **Task tests.** Each flow's task function is a thin wrapper
  around a subprocess call. Unit tests mock the subprocess and
  assert the exact command, cwd, and env that would run.
- **Flow smoke tests.** A synthetic-data run of `daily_refresh`
  against a tiny temp warehouse. Verifies the sequence without
  a Prefect server.
- **CLI tests.** `dbp-orchestrate list`, `dbp-orchestrate run
  <flow> --dry-run`, and argument parsing.

All tests run without a Prefect server or network. The full
test suite must remain runnable on Colab.

### 10. Repository layout

```text
orchestration/
|-- __init__.py
|-- config.py            OrchestrationSettings
|-- alerts.py            on_failure hooks + webhook poster
|-- cli.py               dbp-orchestrate
|-- deployments.py       flow.serve() + schedule registration
`-- flows/
    |-- __init__.py
    |-- ingest.py        ingest_prices / ingest_macro / ingest_sec
    |-- warehouse.py     dbt_build
    |-- quality.py       quality_gate
    |-- features.py      build_features
    |-- backtest.py      run_backtest / track_run
    `-- composite.py     daily_refresh / weekly_refresh / full_refresh
```

Docker compose (M8) gains two services: `prefect-server` and
`prefect-worker`. The project continues to work without them.

## Consequences

### Positive

- The pipeline runs on schedule without manual intervention.
- Transient failures self-heal; terminal failures page a
  human via webhook.
- Every flow is traceable: Prefect run IDs can be cross-
  referenced against the structured logs from M1-M6.
- The orchestration layer adds no logic. It wraps existing
  CLIs, so a bug in orchestration cannot corrupt the
  warehouse — it can only fail to run a stage.
- CI and Colab run the ephemeral mode; no server required.

### Negative

- Prefect 3 is a heavy dependency (~80 MB installed).
  Mitigated by making it an `[orchestration]` extra and by
  the ephemeral mode that does not need a server.
- Prefect's own logging, retry, and persistence are more
  complex than the pipeline itself. Contributors must learn
  the API to debug a flow, not just the CLI.
- Cron is UTC-only. A future maintainer in a different
  timezone must compute offsets by hand. Documented in
  `docs/runbook.md`.

### Neutral

- Prefect Cloud, custom backfill, and DAG branching are out
  of scope. They would require an ADR amendment.

## Alternatives considered

| Alternative | Why rejected |
|-------------|--------------|
| Airflow | Heavier, scheduler requires more infra, overkill for 8 flows |
| Dagster | Asset-first model is elegant but adds concepts that do not pay off at this scale |
| Cron + shell scripts | No retry, no observability, no run history; reinventing Prefect badly |
| GitHub Actions schedule | Runs on GitHub infrastructure; awkward for local DuckDB; no retry semantics worth speaking of |
| Prefect 2 | Superseded; 3.x API is cleaner and closer to our mental model |
| Prefect Cloud | Paid, not needed for a single-developer project |
| Custom Python scheduler | Reinventing the wheel; no UI, no run history |
| Airflow on Kubernetes | Massive ops burden for a project with a single worker |

## References

- Prefect 3.x documentation:
  <https://docs.prefect.io/3.0/>.
- Prefect 3 release notes: `@task` retry semantics,
  `flow.serve()` for ephemeral+served unification.
- Prefect blocks for secrets: out of scope; we use env vars
  (matches ADR 0015 for MLflow tracking URI).

