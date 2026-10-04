# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (M6 — MLflow tracking)

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

