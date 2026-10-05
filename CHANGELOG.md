# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Milestones M1–M5 predate the current changelog format; their history is
recorded in git (`git log --oneline`) and in the ADR index at
`docs/adr/README.md`. From M6 onward, each milestone corresponds to a
minor version (`0.<M>.0`).

## [Unreleased]

## [0.10.0] - 2026-10-05

### Added (M10 — see ADR index for full context)


- **ADR 0019** — monitoring and dashboard design (10 decisions:
  three signals only, pure functions + IO at edges, freshness via
  MAX(trade_date) per mart, drift via PSI + KS, reference = first 60
  trading days, performance from M5 artifacts, one JSON report,
  single-file Streamlit dashboard, dashboard reads monitoring.*
  only, no scheduling).
- `monitoring/config.py` — `MonitoringSettings` (`DBP_MON_*`),
  `MartThreshold` with warn<fail validator, `warehouse_schema`.
- `monitoring/freshness.py` — per-mart `MAX(trade_date)` age vs
  WARN/FAIL thresholds; pure.
- `monitoring/drift.py` — PSI (quantile-bucketed distribution
  distance) + KS (Kolmogorov-Smirnov p-value); flagged when either
  crosses the fail threshold; pure.
- `monitoring/performance.py` — rolling window (default 4 folds)
  over the M5 `metrics.parquet`; thresholds are loose by design;
  pure except `check_run_dir`.
- `monitoring/report.py` — assembles the three sections into one
  JSON report; each section isolated with try/except so a broken
  section does not abort the others.
- `monitoring/cli.py` — `dbp-monitor run | schema | info`.
- `app/streamlit_app.py` — single-file 4-tab dashboard (Health,
  Backtest, Drift, About); cached 600s; reads monitoring.* only.
- Makefile: `monitor`, `monitor-json` targets.
- 79 unit tests in `tests/unit/test_monitoring_*.py`.

### Notes (M10)

- **Colab cannot run the Streamlit server.** Dashboard verification is
  static: import + mock-call each render function against synthetic
  reports. End-to-end `make app` is a user action.
- **No scheduling.** `make monitor` is manual. Wiring monitoring to
  Prefect is a future ADR.
- **Missing warehouse or missing backtest run = FAIL, not skip.**
  Insufficient data for drift (fewer than 2 windows) = PASS with
  reason, because drift is unmeasurable, not failing.
- `docs/adr/` count is now 0001–0019.

### Notes (M9)

- Branch protection (required status checks, dismiss stale reviews,
  require resolved conversations) is configured in GitHub
  repository settings. The exact steps are in
  `docs/runbook.md` under "CI / branch protection".
- Integration tests (`pytest -m integration`) stay local-only until
  M10's monitoring clarifies what they should cover in CI.

### Notes

- **Colab cannot run Docker.** M8 verification is static:
  `yaml.safe_load` on compose, Dockerfile structure assertion,
  `bash -n` on entrypoint, `make -n` on new targets. End-to-end
  `make up` is documented as an acceptance test in
  `docs/runbook.md`.
- **`.env` is not bind-mounted.** Compose loads it via
  `env_file.required: false` and exports it to the container
  environment; pydantic-settings reads from `os.environ`. This
  keeps `make up` working on a fresh clone that has no `.env` yet.
- `docs/adr/` count is now 0001–0017.

## [0.9.0] - 2026-10-05

### Added (M9 — see ADR index for full context)


- **ADR 0018** — CI contract (9 decisions: three parallel jobs
  with timeouts, coverage floor 70%, fixture-only dbt in CI,
  docker-build job with smokes, branch protection documented in
  runbook, Dependabot for pip + Actions, minimal PR template,
  maintenance policy).
- `.github/workflows/ci.yml` — rewritten:
  - `lint-and-test`: adds `--cov-fail-under=70` and uploads the
    `.coverage` file as an artifact.
  - `dbt-build`: unchanged semantics, named job, 15 min timeout.
  - `docker-build` (new): builds the image with Buildx + GHA cache,
    runs three smokes (imports, orchestration CLI, non-root
    entrypoint). 20 min timeout.
  - Top-level `permissions: contents: read`.
- `.github/dependabot.yml` — weekly pip + github-actions PRs,
  grouped by ecosystem, ignoring major bumps of mlflow / prefect /
  pandera.
- `.github/pull_request_template.md` — 4-prompt checklist.
- `Makefile`: `ci` now includes the coverage floor; new targets
  `ci-docker` and `ci-full`.
- `pyproject.toml`: coverage `source` extended to include
  `tracking`, `orchestration`, and `quality`.

## [0.8.0] - 2026-10-05

### Added (M8 — see ADR index for full context)


- **ADR 0017** — Docker design (12 decisions: python:3.11-slim,
  single-stage, non-root, cache-friendly deps, bind-mounted state,
  two services with profiles, `sleep infinity` default, optional
  bootstrap entrypoint, no published ports by default, Make targets
  wrap compose, static verification only).
- `Dockerfile` — Python 3.11-slim, non-root `app` user, `tini` PID 1,
  stub-package dependency warm-up, editable install with
  `[dbt,quality,backtest,tracking,orchestration]` extras.
- `.dockerignore` — excludes `.git`, caches, local state, secrets.
- `scripts/docker_entrypoint.sh` — optional `DBP_DOCKER_BOOTSTRAP=1`
  bootstrap (mkdir state dirs, verify importable, warn on missing
  `.env`), then `exec "$@"`.
- `docker-compose.yml` — `worker` service (always up, no ports) +
  `prefect-server` service (profile `served`, port `:4200`).
- Makefile: 10 new targets — `docker-build`, `up`, `down`, `logs`,
  `shell`, `run`, `pipeline`, `prefect-up`, `prefect-down`,
  `orchestrate`.

## [0.7.0] - 2026-10-04

### Added (M7 — see ADR index for full context)


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

## [0.6.0] - 2026-10-04

### Added (M6 — see ADR index for full context)


- **ADR 0015** — MLflow tracking + Model Registry design (8 decisions:
  SQLite backend, single experiment, run_name = M5 run_id, prefixed
  params, namespaced metrics, artifact reuse, refit-on-full-data +
  @challenger/@champion, opt-in `--mlflow`).
- `tracking/config.py` — `TrackingSettings` (`DBP_TRACK_*` env prefix,
  URI scheme validator).
- `tracking/client.py` — optional MLflow import guard, repo-aware SQLite
  URI resolution, idempotent experiment setup, run context manager,
  best-effort git info/diff + pip freeze helpers.
- `tracking/logger.py` — params (54 fields), metrics (41 fields,
  `pooled/*`, `deflated/*`, `agg/*`, `baseline/<name>/*`, `fold/<id>/*`),
  and artifact upload from `data/backtest/{run_id}/`.
- `tracking/registry.py` — `refit_full_model` (no MLflow dep),
  `register_model` (logs to a separate registry-purpose run), and
  `log_and_register_model` orchestrator. Alias `@challenger` moved
  automatically; `@champion` reserved for manual promotion.
- `tracking/cli.py` — `dbp-tracking list-runs | best-run | compare`.
- `scripts/run_backtest.py` — new flags `--mlflow`, `--no-mlflow`,
  `--tracking-uri`, `--no-register-model`. Default remains off; the
  pipeline runs unchanged without MLflow installed.
- 78 unit tests in `tests/unit/test_tracking_*.py`.
- `[tracking]` extra in `pyproject.toml` brings in `mlflow`; the
  `tracking*` package is now part of setuptools discovery.

### Fixed (M6 discovered)

- **`register_model` failed on MLflow 3.x** with
  "Untrusted types found in the file: ['numpy.dtype']" because MLflow 3
  changed the default sklearn serialization to `skops`. Fix: pass
  `skops_trusted_types=["numpy.dtype"]` and switch from the deprecated
  `artifact_path=` to `name=`.
- **`get_git_info` raised outside a repo.** `log_backtest_run` crashed
  when the CLI was launched from a cwd that was not a git repository
  (e.g. running the backtest against data in `/tmp`). Git info/diff
  helpers are now strictly best-effort, and `_run_tracking` receives
  `repo_root=REPO` explicitly.

### Changed

- `docs/adr/` count is now 0001–0015.

[0.10.0]: https://github.com/WoodinGlass/data-backtest-pipeline/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/WoodinGlass/data-backtest-pipeline/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/WoodinGlass/data-backtest-pipeline/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/WoodinGlass/data-backtest-pipeline/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/WoodinGlass/data-backtest-pipeline/releases/tag/v0.6.0
[Unreleased]: https://github.com/WoodinGlass/data-backtest-pipeline/compare/main...HEAD
