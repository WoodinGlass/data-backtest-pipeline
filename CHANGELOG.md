# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (M7 — Prefect orchestration)

- **ADR 0016** — Prefect 3 orchestration design (10 decisions:
  ephemeral default, SQLite storage, one flow per stage, three
  composites, cron UTC schedules, 3x retry with [10, 60, 300]s delays,
  best-effort webhook alerts, no parallelism).
- `orchestration/config.py` — `OrchestrationSettings` (`DBP_ORCH_*`).
- `orchestration/_compat.py` — Prefect-optional `@flow`/`@task`
  decorators; no-op when Prefect is missing.
- `orchestration/_subprocess.py` — `run_command` wrapper: capture,
  log tail, raise CalledProcessError on non-zero.
- `orchestration/alerts.py` — failure payload builder, best-effort
  webhook poster, Prefect on_failure hook, `Timer` helper.
- `orchestration/deployments.py` — schedule registry + `serve_flows`
  for served mode (blocking; Docker-targeted).
- `orchestration/cli.py` — `dbp-orchestrate list | run | info | schedule`.
- `orchestration/flows/`:
  - `ingest.py` — `ingest_prices`, `ingest_macro`, `ingest_sec`
  - `warehouse.py` — `dbt_build`
  - `quality.py` — `quality_gate`
  - `features.py` — `build_features`
  - `backtest.py` — `run_backtest` (with `--mlflow` from M6)
  - `composite.py` — `daily_refresh`, `weekly_refresh`, `full_refresh`
- 88 unit tests in `tests/unit/test_orchestration_*.py`.
- `pyproject.toml`: `prefect>=3.0,<4.0`; `orchestration*` package added
  to setuptools discovery.

### Changed

- **ADR 0016 §4 simplified.** The original design listed a separate
  `track_run` flow. Since tracking is a flag on `scripts/run_backtest.py`
  (not a separate stage), and since sharing a `RunResult` across
  process boundaries would have been the only reason for a second
  flow, `track_run` was merged into `run_backtest`. See ADR 0016
  design notes.
- `docs/adr/` count is now 0001–0016.

