# Predicting Daily Equity Direction: A Reproducible, Leakage-Free Pipeline

**A research-style summary of the `data-backtest-pipeline` project.**

---

## Abstract

We describe a reproducible, leakage-free pipeline that predicts the
sign of the next-day return for 31 large-cap US equities, using
point-in-time prices, vintage-aware macro data, and filing-date-aware
fundamentals. The pipeline is engineered to be auditable: every number
in the backtest can be traced back to an immutable raw snapshot, a
tested dbt transformation, and a versioned model.

The primary contribution is **not** a novel predictive signal. It is
an **engineering artifact**: a walk-forward backtest where
survivorship, look-ahead, and vintage bias are explicitly addressed,
and where a null result is the anticipated outcome. The project
demonstrates that the honest number — often an AUC near 0.51 and a
Sharpe indistinguishable from zero after costs — is more valuable than
an inflated one produced by a leaky pipeline.

---

## 1. Problem statement

> Can a well-engineered, point-in-time data pipeline produce a
> calibrated probabilistic classifier that predicts the direction of
> next-day returns for a small universe of US equities, and does that
> classifier beat a naive buy-and-hold benchmark after realistic
> transaction costs?

Sub-questions:

1. **Reproducibility.** Can the backtest be re-run from a clean
   checkout and produce identical numbers?
2. **Leakage.** Are all features strictly point-in-time, and are
   labels at `t` only observable at `t+1`?
3. **Calibration.** If a classifier outputs `p = 0.6`, is the
   realized frequency close to 60%?
4. **Honesty.** If the classifier does not beat buy-and-hold, does
   the pipeline report that clearly?

---

## 2. Data

### Universe

31 large-cap, highly liquid S&P 500 constituents plus SPY as
benchmark. Defined in `config/universe.txt` and `seeds/ticker_metadata.csv`.
Point-in-time membership intervals (`valid_from`, `valid_to`) prevent
delisted tickers from being silently included — see ADR 0005.

### Sources

| Layer | Source | Volume | Vintage handling |
|---|---|---|---|
| Prices | yfinance | 94,528 OHLCV rows | Append-only, no vintage |
| Macro | FRED + ALFRED | 23.7M vintage observations, 140 series | Per-release vintage (ADR 0009) |
| Fundamentals | SEC EDGAR XBRL | 904,127 facts, 137 curated tags | Per filing, keyed by `filed` (ADR 0010) |

### Point-in-time contract

- **Prices:** features at `t` use only bars up to and including `t`.
- **Macro:** the vintage current at `t` is used, not the latest
  revision.
- **Fundamentals:** the latest formal filing (10-K / 10-Q) whose
  `filed` date is `<= t`.

Every layer is either append-only (raw) or deterministically rebuilt
(dbt). No silent forward-fill. No use of revised data in a historical
backtest.

---

## 3. Features

Twenty-three features across five families, all in
`data/features/v1/features_daily.parquet`:

| Family | Count | Examples | Source |
|---|---|---|---|
| `px_*` | 8 | lagged returns 1/5/20d, vol 20/60d, RSI(14), momentum 60d, volume ratio | prices |
| `cs_*` | 2 | percentile rank within sector and universe | prices (cross-sectional) |
| `mkt_*` | 2 | rolling beta and correlation vs SPY | prices |
| `mc_*` | 6 | Fed funds, DGS10, DGS2, CPI YoY, payrolls YoY, unemployment | macro (vintage-aware) |
| `fd_*` | 5 | net margin, ROE, capex intensity, revenue YoY, employees | fundamentals (filing-date PIT) |

**Anti-leakage tests.** For every family, a poison-future probe
poisons rows after date `K` with garbage, recomputes, and asserts
that features at or before `K` are unchanged. See
`tests/unit/test_*_features.py`.

---

## 4. Model

A **logistic regression** with `StandardScaler` and median imputation,
trained per fold with **time-decay sample weights** (half-life 252
trading days). Fixed `C = 0.1`. No inner hyperparameter search in v1
— the goal is a clean baseline, not a tuned one. See ADR 0014 §3.

Predictions are calibrated by construction (logistic regression is a
probabilistic classifier trained on log loss). Calibration is
measured, not assumed: the backtest reports calibration slope,
intercept, and a 10-bin reliability curve.

The model outputs `p = P(next_return > 0)` per (ticker, trade_date).

---

## 5. Risk framework

Signals become positions via a locked contract (ADR 0013):

| Component | Rule |
|---|---|
| Entry | `p > 0.55` goes long; otherwise flat |
| Staking | Fractional Kelly, `k = 0.25`, per-name cap 5% NAV |
| Volatility target | Portfolio-level, 10% annualized, 20-day lookback; only scales down |
| Stop loss | Per-position −8%, 5-day cooldown |
| Drawdown | Derisk 50% at −10%; halt at −20% |
| Costs | 2.5 bp one-way (commission + slippage) |
| Rebalance | Threshold: only trade if target differs from current by >1% NAV |
| Direction | Long + flat (no short) |

The framework is **pure**: no state across days, no magic numbers
outside `risk/config.py`.

---

## 6. Backtest methodology

**Walk-forward** with the following locked protocol (ADR 0014):

- **Split:** rolling 36-month train, 3-month test, 3-month step.
- **Purge + embargo:** 1 row dropped at train end (label horizon), 5
  additional trading days of gap before test.
- **Folds:** ~35, spanning 2018-Q1 through 2026-Q4.
- **Metrics:** per fold (median + IQR), pooled (concatenated), yearly,
  bootstrap CI (1000 resamples, block=5), **Deflated Sharpe Ratio**
  (Bailey & López de Prado 2014).
- **Baselines:** B0 naive (p=0.5), B1 momentum 20d, B2 SPY
  buy-and-hold, B3 always-long top-10.

The walk-forward is single-pass, deterministic, and reproducible. No
shuffling. No use of future folds to inform past fits.

---

## 7. Results

### 7.1 Expected outcomes (stated a priori)

Stated **before** observing any real backtest numbers, based on the
literature on daily equity direction prediction:

| Metric | Expected range | Rationale |
|---|---|---|
| AUC | **0.50 – 0.53** | Daily direction is close to a martingale |
| Log loss | **0.690 – 0.693** | Barely below the 0.6931 of the naive 50/50 |
| Brier | **0.248 – 0.251** | Same margin |
| Hit rate | **0.50 – 0.51** | |
| Sharpe (after cost) | **−0.3 – +0.5** | Weak signal + 5 bp round-trip |
| Excess ROI vs SPY | **−10% – +5%** | Likely underperformance |
| Calibration slope | **0.8 – 1.2** | Logistic regression, well-calibrated by design |

These are stated first so that they cannot be retrofitted to whatever
the numbers end up being. A materially higher AUC would be a signal
of a bug, not a triumph.

### 7.2 Observed outcomes

> **To be filled after running the full pipeline on a local machine
> with network access.**
>
> The Colab environment this project was developed in cannot run the
> full ingest (yfinance + FRED + ALFRED + SEC EDGAR ≈ 30–60 minutes
> plus external API rate limits). The `make pipeline` target runs the
> full sequence locally; `make monitor` and `make app` surface the
> results once artifacts exist under `data/`.

Placeholder table (to be replaced after the local run):

| Model | Log loss | Brier | AUC | Hit rate | Sharpe | Max DD | ROI vs SPY |
|---|---|---|---|---|---|---|---|
| Naive 50/50 | — | — | — | — | — | — | — |
| Momentum (last return) | — | — | — | — | — | — | — |
| Buy-and-hold SPY | — | — | — | — | — | — | 0.00 |
| Prices-only model | — | — | — | — | — | — | — |
| Prices + macro + fundamental | — | — | — | — | — | — | — |

Calibration curve: `docs/preview/calibration.png` (generated by
`make backtest`).
Cumulative returns vs SPY: `docs/preview/equity_curve.png`.

### 7.3 What can already be said

Independently of the observed numbers:

- **The pipeline is reproducible.** Every raw snapshot is
  content-addressed. Re-running ingestion is idempotent
  (pass 1 writes, pass 2 skips). dbt models are deterministic. The
  backtest sets a global seed.
- **The pipeline is leakage-free by construction.** Purge + embargo
  at the train/test boundary; PIT contracts enforced by singular dbt
  tests and by anti-leakage unit tests on every feature family.
- **The pipeline is auditable.** Every metric in `summary.json` can
  be traced to a specific fold, a specific dbt model, a specific raw
  snapshot.
- **Baselines are measured with the same protocol as the main
  model.** B2 (SPY buy-and-hold) is the benchmark; B0, B1, B3
  bound the space of "signal-free" strategies.

---

## 8. Limitations

### Data

- **Universe size.** 31 tickers is small. Even with PIT membership
  and no explicit survivorship, sample-size noise in the per-fold
  metrics is significant.
- **Time span.** 2015–2026 covers a specific regime. No dot-com, no
  2008, no 1987. The walk-forward trains only on what it has seen.
- **yfinance is not production-grade.** Vendor-derived prices can
  change without notice; `adj_close` is recomputed on each fetch.
  This is documented and excluded from the content hash (ADR 0006).
- **Fundamental coverage is 52.2%.** Roughly half of
  (ticker, trade_date) pairs have no fundamental data — because the
  ticker had not yet filed, or because the tag is not in the curated
  registry. Downstream features must tolerate NULLs.
- **Macro ingest is slow (~15 min).** The cost of vintage-aware
  ingestion: 95 series carry full ALFRED history.

### Methodology

- **Time-decay half-life is a judgment call (252 days).** Sensitivity
  is not swept in v1.
- **Rolling 36-month train window is a judgment call.** Expanding
  window would use more data but assume stationarity.
- **Deflated Sharpe uses N=4** (main + 3 baselines). A larger
  sweep would require a larger N.

### Model

- **Logistic regression cannot capture feature interactions.**
  Gradient boosting is the natural next step, deferred to a future
  iteration.
- **Fixed `C=0.1`** is a prior, not a tuned value.

### Deployment

- **The public dashboard runs on synthetic data.** The real backtest
  is only run locally. See ADR 0020 §3.

---

## 9. Conclusion

We built a pipeline where **every failure mode that inflates a
backtest has a named mitigation**:

| Failure mode | Mitigation |
|---|---|
| Look-ahead bias | PIT contracts at every layer; anti-leakage tests |
| Survivorship bias | Universe defined as-of date (ADR 0005) |
| Vintage bias | ALFRED vintage-aware macro (ADR 0009) |
| Filing-lag bias | Fundamental PIT by `filed` (ADR 0010) |
| Overfitting | Walk-forward, purge + embargo, Deflated Sharpe |
| Non-reproducibility | Content-addressed raw, deterministic dbt, seeds |
| Silent data corruption | Three-layer quality defense (ADR 0008) |
| Cost-blind backtest | 5 bp round-trip charged per trade |

The expected outcome — a classifier barely better than chance — is
the correct outcome. The project's value is that this outcome, when
it appears, will be **believable**.

---

## 10. Reproduction

### Requirements

- Python 3.11
- Make
- (Optional) Docker for `make up`
- (Optional) A FRED API key

### From a clean checkout

```bash
git clone https://github.com/WoodinGlass/data-backtest-pipeline.git
cd data-backtest-pipeline
cp .env.example .env          # edit if you have a FRED_API_KEY

# Local: install + run
make setup-dev
make pipeline                 # ingest -> dbt -> quality -> features -> backtest -> track

# Or Docker:
make up
make pipeline
```

### Expected artifacts

After `make pipeline`:

- `data/warehouse.duckdb` — the warehouse
- `data/features/v1/features_daily.parquet` — 23-feature PIT table
- `data/backtest/<run_id>/` — per-fold predictions, returns, metrics,
  plots, summary
- `mlruns/` — MLflow tracking store (if `--mlflow` was passed)
- `reports/monitoring.json` — freshness + drift + performance report

### Reproducing the summary numbers

```bash
make backtest                  # walk-forward, saves a run dir
make monitor                   # text summary of the latest run
make app                       # open the dashboard
```

To verify the raw layer is idempotent:

```bash
make ingest                    # first pass
make ingest                    # second pass — should report "skipped" for every ticker
```

### Public dashboard

The Streamlit Cloud deployment renders from `data_demo/` (synthetic
sample data). URL: *(to be filled after deploy; see `docs/runbook.md`
"Streamlit Cloud deployment")*.

---

## References

- Bailey, D. H., & López de Prado, M. (2014). *The Deflated Sharpe
  Ratio*. Journal of Portfolio Management.
- Harvey, C. R., & Liu, Y. (2015). *Backtesting*. Journal of
  Portfolio Management.
- López de Prado, M. (2018). *Advances in Financial Machine
  Learning*. Wiley.
- Politis, D. N., & Romano, J. P. (1994). *The Stationary Bootstrap*.
  JASA.
- The project's own ADR index: `docs/adr/README.md`.
