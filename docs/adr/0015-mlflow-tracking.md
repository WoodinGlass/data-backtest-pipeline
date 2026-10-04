# ADR 0015: MLflow Tracking and Model Registry

- **Status:** Accepted
- **Date:** 2026-10-04
- **Related:** ADR 0014 (walk-forward methodology)

## Context

M5 produces every backtest artifact (config, split, predictions,
returns, metrics, plots) as files under `data/backtest/{run_id}/`.
That is enough to reproduce a run from a shell, but it is a poor
tool for:

- **Comparing** two runs side by side (which fold changed? which
  metric moved?).
- **Auditing** what code + config produced a given Sharpe.
- **Registering** the model that passed evaluation as a named,
  versioned artifact for later deployment.

Doing this manually (spreadsheets, dir diffs, ad-hoc JSON) does not
scale past a handful of runs. M6 adds MLflow as the tracking layer.

## Decision

We integrate MLflow with eight locked decisions. All are
implemented under `tracking/`.

### 1. Backend: local SQLite, file-based fallback

Default tracking URI: `sqlite:///mlruns/mlflow.db`.

Rationale:
- **Model Registry requires a database backend.** The file-based
  store (`./mlruns` directory) does not support registering models,
  only tracking runs. Since we want a registry (decision 7), SQLite
  is the minimum.
- SQLite is single-file, zero-server, and portable. Same trade-off
  as ADR 0002 (DuckDB) for the warehouse.
- The default is overridable via `DBP_TRACKING_URI` for a shared
  server later.

`mlruns/` is git-ignored. Artifacts default to
`mlruns/{experiment_id}/{run_id}/artifacts/`.

### 2. Single experiment: `daily-direction`

One experiment for the whole project. Runs are differentiated by
params, not by experiment name. This makes `mlflow ui` a single
view of all history.

### 3. Run name = M5 `run_id`

`run_id` from `backtest.runner` (`YYYYMMDD_HHMMSS_logistic_C0.1_hl252`)
becomes the MLflow run name. This guarantees a 1:1 mapping between
MLflow runs and the on-disk artifact directory. Any MLflow run can
be traced to `data/backtest/{run_id}/` and vice versa.

### 4. Params: flattened, prefixed

All `BacktestSettings` and `RiskSettings` fields are logged as
params with prefixes:

- `bt.train_window_months = 36`
- `bt.model_C = 0.1`
- `risk.kelly_fraction = 0.25`
- `git.sha = abc1234`, `git.dirty = false`
- `env.python_version = 3.11.9`

Flat names are easy to filter (`params.bt.model_C = "0.1"`) and
easy to render in the MLflow compare view. Prefixes group fields
in the UI.

### 5. Metrics: namespaced with `/`

Primary metrics use a `pooled/` prefix; per-fold use `fold/<k>/`;
aggregated use `agg/`; deflated Sharpe uses `deflated/`.

```
pooled/sharpe
pooled/auc
pooled/log_loss
pooled/sharpe_ci_lower
pooled/sharpe_ci_upper
deflated/sharpe_annualized
deflated/deflated_sharpe
agg/sharpe_median
agg/sharpe_iqr
fold/00/sharpe
fold/00/auc
...
fold/34/sharpe
fold/34/auc
```

Rationale: MLflow renders `/` as nested groups. Per-fold metrics
are limited to the two that matter most for a first look (Sharpe
and AUC); everything else is already in the artifacts. This keeps
the metric count per run under ~120 (35 folds × 2 + ~50 pooled)
even for a full 2018–2026 run.

### 6. Artifacts: reuse M5 output directory

The entire `data/backtest/{run_id}/` directory is uploaded as
MLflow artifacts under `backtest/`. Zero rework; M5 already writes
config.json, split.json, predictions/*, returns/*, metrics.parquet,
summary.json, tables, plots.

Additional artifacts uploaded from the tracking layer:

- `git.diff` — `git diff HEAD` at run time (empty if clean).
- `env.txt` — `pip freeze` output (top 50 packages).

Rationale: `git.sha` alone does not capture uncommitted edits. The
diff catches the "I forgot to commit" case.

### 7. Model Registry: refit on full data, auto-increment

After a successful walk-forward, `tracking.registry.refit_full`
trains one final model on **all features** with the same
`BacktestSettings` (same `C`, same decay half-life, same features).
This is the model that ships.

It is registered under `daily-direction-model`. Versions
auto-increment (v1, v2, ...). Aliases:

- `@challenger` — automatically moved to the newest version.
- `@champion` — **never moved automatically**. Promotion is manual
  (`mlflow.aliases.set_alias` or via the UI), typically after M12.

Rationale: per-fold models are not portable for deployment (35 of
them). Refitting on all data yields one serializable model whose
training data and config are already recorded in the run. The
`challenger` vs `champion` split prevents an accidental "newest
beats old" takeover.

The refit is logged as a *separate* MLflow run with tag
`purpose=registry`, so the walk-forward run stays clean.

### 8. Opt-in via `--mlflow` flag; no hard dependency

`scripts/run_backtest.py` gets a new flag:

```
python scripts/run_backtest.py --mlflow
python scripts/run_backtest.py --mlflow --tracking-uri sqlite:////abs/path.db
python scripts/run_backtest.py                     # M5 behavior, no MLflow
```

Implementation: `tracking/__init__.py` exposes `log_backtest_run`
that no-ops when MLflow is not installed (ImportError guard) or
when the caller passes `enabled=False`.

Rationale: the pipeline must run end-to-end without MLflow for
minimal Colab sessions and for CI jobs that do not need tracking.
MLflow is an enhancement, not a dependency. This mirrors the
existing pattern where `pandera`, `dbt`, and `prefect` are optional
extras.

## Consequences

### Positive

- One command (`mlflow ui --backend-store-uri sqlite:///mlruns/mlflow.db`)
  gives a full interactive history of every backtest.
- Runs are comparable: the UI diffs params and metrics across runs.
- Every run is traceable to a git SHA + uncommitted diff.
- Model Registry gives a first-class place for "the model that
  ships", with a safe promotion workflow.

### Negative

- SQLite backend does not support concurrent writes. This is fine
  for a single-developer project; a shared server is a future ADR
  if multiple contributors start running backtests in parallel.
- Refit-on-full-data produces a model whose validation metric is
  the walk-forward Sharpe, not the refit's own metric. This is
  standard practice but must be documented in the model card
  (M12).
- MLflow is an extra dependency (~150 MB installed). Mitigated by
  making it optional (`[tracking]` extra) and by the `--mlflow`
  opt-in flag.

### Neutral

- MLflow Projects, model serving, and hyperparameter sweeps are
  out of scope for M6. They will be considered in M5.5 and M12 if
  warranted.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| File-based MLflow backend | No Model Registry support |
| MLflow remote server | Extra ops burden for a single-dev project |
| Custom JSON tracking | Reinventing the wheel; no UI |
| Weights & Biases | External dependency, paid for private projects |
| DVC | Focused on data versioning, not experiment tracking |
| Log everything to git | Binary artifacts bloat the repo; no compare UI |
| Auto-promote newest model to `@champion` | Accidental regression risk |
| Log every per-fold metric (log loss, Brier, ...) | 35 folds × 8 metrics × 2 models = 560 metrics per run; UI becomes unusable |

## References

- MLflow documentation, *Tracking* and *Model Registry*.
- MLflow 2.x, `mlflow.set_tracking_uri`, `mlflow.set_experiment`,
  `mlflow.start_run`, `mlflow.log_params`, `mlflow.log_metrics`,
  `mlflow.log_artifacts`, `mlflow.sklearn.log_model`,
  `mlflow.register_model`, `MlflowClient.set_registered_model_alias`.
- Sculley et al. (2015). *Hidden Technical Debt in Machine Learning
  Systems*. NeurIPS. (Motivation for tracking + registry.)
