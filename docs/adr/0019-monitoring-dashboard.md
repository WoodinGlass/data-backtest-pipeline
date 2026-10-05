# ADR 0019: Monitoring and Streamlit Dashboard

- **Status:** Accepted
- **Date:** 2026-10-05
- **Related:** ADR 0008 (quality gate), ADR 0014 (walk-forward),
  ADR 0015 (MLflow), ADR 0016 (Prefect), ADR 0018 (CI contract)

## Context

By M9 the pipeline produces a running warehouse, a versioned feature
table, tracked backtest runs, and a registered model. What is
missing is a **read-only view** of pipeline health that answers
three questions in one look:

1. Is the data fresh? (freshness)
2. Has the input distribution shifted? (drift)
3. Is the model still predictive? (performance)

Without this, the only way to answer any of those questions is to
open a DuckDB shell, remember which table holds what, and compute
the metric by hand. That does not scale past the maintainer.

M10 adds a monitoring layer and a Streamlit dashboard. It must
**not** add alerting (M7 webhook already covers failure
notification), must **not** retrain or promote models (M12 owns
that), and must **not** require a persistent time-series store.

## Decision

We adopt ten decisions.

### 1. Three signals, no more

| Signal | Question | Frequency |
|---|---|---|
| Freshness | How old is the newest row per mart? | daily |
| Drift | Has feature distribution shifted vs a reference window? | weekly |
| Performance | What are the recent rolling metrics? | weekly |

Rationale: three signals are enough to catch the failure modes
that actually matter (stale data, silent regime change, model
decay). Adding a fourth (e.g. anomaly detection) buys complexity
without a clear failure mode to catch.

### 2. Pure functions; IO at the edge

Every monitor is a pure function:

```
freshness.check(df, settings)      -> dict
drift.check(ref_df, cur_df, ...)   -> dict
performance.check(metrics_df, ...) -> dict
```

IO (reading the warehouse, writing JSON) lives in `monitoring/report.py`
and `monitoring/cli.py`. Unit tests exercise the pure functions
with synthetic DataFrames; no warehouse required.

Rationale: same pattern as `backtest/metrics.py` and
`risk/staking.py`. Testable in milliseconds, no fixtures to maintain.

### 3. Freshness: `MAX(trade_date)` per mart

For each mart listed in `MonitoringSettings.marts`, compute:

```
age_days = reference_date - max(trade_date)
status   = PASS | WARN | FAIL  based on per-mart thresholds
```

Default thresholds (calendar days):

| Mart | WARN | FAIL |
|---|---|---|
| `fct_prices_daily` | 3 | 7 |
| `fct_returns_daily` | 3 | 7 |
| `fct_macro_daily` | 45 | 90 |
| `fct_fundamentals_daily` | 120 | 180 |

Rationale: prices update daily, macro monthly, fundamentals
quarterly. One number cannot fit all three; a table is the minimum
honest representation.

### 4. Drift: PSI + KS, two numbers not one

For each numeric feature, compare the **reference** window (first
60 trading days of the feature table) against the **current** window
(last 60 trading days):

- **PSI** (Population Stability Index): bucketed distribution
  distance, unbounded, thresholds 0.1 (small), 0.25 (large).
- **KS** (Kolmogorov-Smirnov): max CDF distance + p-value,
  bounded [0, 1].

Both are reported. A feature is flagged when **either** PSI > 0.25
**or** KS p-value < 0.01. Rationale: PSI catches shape changes that
KS smooths over; KS catches location changes that PSI's bucketing
can miss.

No MMD, no adversarial validation, no autoencoder. Those methods
answer "is there any difference" better than "is there a difference
that matters"; the first question is not useful for a daily equity
pipeline where everything drifts a little.

### 5. Drift reference: first 60 trading days, no snapshot file

Reference = the **first 60 trading days** present in the feature
table. Current = the **last 60 trading days**. Both windows are
60 rows per ticker.

Rationale: deterministic, no external snapshot to maintain, no
"training data version" to reconcile. The trade-off is that the
reference is arbitrary — it is "the beginning of the data", not
"the training set". For a walk-forward backtest that is fine: every
fold retrains on a rolling window anyway, so there is no single
training distribution to drift *from*.

A future ADR can add a "snapshot reference" option if the project
grows a fixed training set.

### 6. Performance: read M5 artifacts, do not retrain

Performance reads `data/backtest/{latest_run}/metrics.parquet`
(M5 output) and computes rolling aggregates over the last N folds:

| Metric | Window |
|---|---|
| log loss | last 4 folds |
| Brier | last 4 folds |
| AUC | last 4 folds |
| Sharpe | last 4 folds |

Rolling window size is configurable via `MonitoringSettings.perf_window_folds`.

Rationale: retraining inside a monitor would duplicate M5, would
blur the line between monitoring and research, and would not be
reproducible without the seed and config of the original run. The
metrics.parquet is already there and already versioned.

### 7. Report: single JSON + text summary

`dbp-monitor` writes `reports/monitoring.json`:

```json
{
  "generated_at": "2026-10-05T22:00:00Z",
  "reference_date": "2026-10-05",
  "freshness": {"marts": [...], "overall": "PASS"},
  "drift": {"features": [...], "flagged_count": N, "overall": "PASS"},
  "performance": {"metrics": {...}, "overall": "PASS"}
}
```

`make monitor` prints a text summary to stdout and (optionally)
writes the JSON with `--json PATH`.

Rationale: one artifact, one schema, grep-able. No HTML email, no
Slack (M7 has a webhook for actual failures; monitoring is a
scheduled review, not an alert).

### 8. Streamlit: one file, four tabs

`app/streamlit_app.py` with `st.tabs(["Health", "Backtest", "Drift", "About"])`.

- **Health**: freshness table, volume sanity, latest run_id.
- **Backtest**: pooled metrics, equity curve vs SPY, calibration
  curve, deflated Sharpe. Reads from `data/backtest/{run_id}/`.
- **Drift**: PSI/KS bar chart per feature, top-N shifted list.
- **About**: repo link, ADR list, package versions.

Rationale: four tabs cover the three signals plus context. A
multi-page router is unnecessary for four views; a single file is
easier to read, easier to test, and easy to deploy to Streamlit
Cloud (M12).

### 9. Dashboard reads from `monitoring/`, not from raw files

`app/streamlit_app.py` imports from `monitoring.*`. It never reads
Parquet directly. This keeps one source of truth for parsing and
metric definitions; if a metric changes, it changes in one place.

### 10. No scheduling in M10

Monitoring runs manually (`make monitor`). Scheduling it via Prefect
would require a new flow (M7 extension) and a decision about what
"monitoring failed" means — that decision belongs in its own ADR.
M10 ships the machinery; a future milestone can wire it to the
scheduler.

## Consequences

### Positive

- Three questions answered in one `make monitor` call and one
  Streamlit page.
- Every metric is testable without a warehouse (pure functions).
- Dashboard has zero business logic; it is a view.
- No new dependency tier: `streamlit` and `scipy` are already in
  existing extras.

### Negative

- The drift reference is arbitrary ("first 60 days"). If the
  earliest data is unusual, drift will over-report. Mitigated by
  documenting the choice and keeping the window size configurable.
- Monitoring is manual. If the maintainer does not run
  `make monitor`, nothing happens. This is acceptable for a
  portfolio project; production deployment is out of scope.
- Streamlit's rerun model (rerun on every widget change) means a
  slow warehouse query will re-execute on every interaction. The
  dashboard caches with `@st.cache_data(ttl=600)` to mitigate.

### Neutral

- No persistent metrics time-series store. Rolling metrics are
  computed on-demand from artifacts. If run history grows large
  enough that on-demand compute is slow, a store is a future ADR.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Grafana + Prometheus | Ops burden for a single-developer project |
| HTML email reports | Cannot be inspected interactively; email deliverability issues |
| Streamlit multi-page app | Extra structure for four views |
| ML-based anomaly detection | No clear failure mode it catches that PSI/KS/freshness miss |
| Retrain-in-monitor | Duplicates M5; blurs monitoring vs research |
| Persistent metric DB | Extra dependency; on-demand compute is fast enough at this scale |
| One dashboard tab | Four views do not fit comfortably; tabs are the minimum structure |
| Auto-remediation (e.g. auto re-ingest on stale data) | Out of scope; M7 webhook notifies a human, who decides |

## References

- Population Stability Index: standard credit-risk technique,
  thresholds 0.1 / 0.25 (Siddiqi, 2006).
- Kolmogorov-Smirnov two-sample test: `scipy.stats.ks_2samp`.
- Streamlit docs: *Caching*, *Tabs*.
