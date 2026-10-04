# ADR 0014: Walk-Forward Backtest Methodology

- **Status:** Accepted
- **Date:** 2026-10-04
- **Related:** ADR 0005 (universe PIT), ADR 0012 (features), ADR 0013 (risk)

## Context

M5 is the first milestone that produces performance numbers. Every
design choice affects whether those numbers can be trusted. The
pipeline already guarantees point-in-time features (M4) and a
leakage-free risk framework (M4.5). M5 must not undo that.

The goal of M5 is **not** to find alpha. It is to produce a number
for "how well does this pipeline predict daily direction" using a
protocol that would survive review. A null result is a valid and
expected outcome.

## Decision

Nine decisions, locked. All implemented in `backtest/config.py`.

### 1. Split: rolling 36m train, 3m test, 3m step

- Rolling (not expanding): assumes drift; old data adds noise.
- Test = 3 months: ~1950 rows per fold, ~33 folds across 2018–2026.
- Step = 3 months: test windows do not overlap.

### 2. Purge + embargo

- **Purge 1 row** at train end (label horizon = 1 day).
- **Embargo 5 trading days** between train end and test start
  (accounts for 60d autocorrelated features).
- Total gap = 6 days. Ref: López de Prado (2018) §7.4.3.

### 3. Model v1: logistic regression

- `LogisticRegression(C=0.1, solver=lbfgs)` + `StandardScaler`.
- No inner hyperparameter search. Fixed `C` for honest N in
  deflated Sharpe.
- GBM deferred to a follow-up amendment if v1 shows signal.

### 4. Sample weighting: time decay

- `w_t = exp(-lambda * (t_max - t))`, `lambda = ln(2) / 252`.
- Half-life 252d, consistent with rolling 3y window.

### 5. Baselines (all evaluated with same risk + cost)

- **B0**: naive 50/50 (no trade).
- **B1**: momentum 20d (`p=1 if px_return_20d > 0 else 0.45`).
- **B2**: **SPY buy-and-hold — the benchmark to beat.**
- **B3**: always-long top-10 by `cs_percentile_rank_universe_20d`.

### 6. Metrics — per fold, then aggregated

Per fold: log loss, Brier, AUC, hit rate, calibration slope +
intercept, Sharpe, CAGR, MDD, turnover, cost-adjusted return,
excess vs SPY.

Aggregation:
- Primary: **median + IQR** across folds.
- Secondary: mean ± std.
- Pooled: concatenated predictions, one Sharpe/AUC.
- Yearly: Sharpe per calendar year.
- **Bootstrap CI** 1000 resamples, block by date.
- **Deflated Sharpe** with N = 4 (B0, B1, B3, main; B2 excluded).

### 7. Integration with `risk/`

`risk/` consumed unchanged:

```
predictions -> risk.entry -> risk.staking -> risk.limits
            -> backtest.portfolio.apply_rebalance_threshold
            -> daily return
```

Idle cash = Fed Funds (`mc_fedfunds`, fallback 0%). Benchmark = SPY.

### 8. Reproducibility

Per run: `data/backtest/{run_id}/` with config.json, split.json,
predictions/, returns/, metrics.json, calibration.parquet,
equity_curves.parquet.

Global seed = 42.

### 9. Scope

Any change to split / model / weighting / aggregation requires a new
ADR or amendment, so results remain comparable across milestones.

## Consequences

**Positive:**
- Every number traceable to this ADR + commit hash.
- Purge + embargo eliminate a class of label leakage.
- Per-fold + median/IQR + pooled + yearly bounds lucky-fold effect.
- Deflated Sharpe + bootstrap CI make "good" numbers harder to claim.

**Negative:**
- ~33 folds takes longer than a single split (~5–15 min).
- Fixed `C=0.1` may underperform a tuned model.
- Time-decay half-life is a judgment call.
- Logistic cannot capture interactions (GBM deferred).

**Neutral:**
- Daily equity direction is close to a martingale. Not beating B2
  is the **expected** outcome.

## Alternatives rejected

| Alternative | Why |
|---|---|
| Expanding window | Assumes stationarity |
| Single 80/20 split | One fold → huge CI, regime-dependent |
| Shuffled k-fold CV | Temporal leakage |
| Purge + embargo, no time decay | Time decay already handles drift |
| Full Bayesian / ensemble v1 | Complexity without evidence |
| Tune `C` and half-life | Multiplies N; deferred |
| Sharpe-only reporting | Ignores calibration |

## References

- Bailey & López de Prado (2014). Deflated Sharpe Ratio. JPM.
- Harvey & Liu (2015). Backtesting. JPM.
- López de Prado (2018). Advances in Financial ML. Wiley. §7, §11–12.
- Politis & Romano (1994). Stationary Bootstrap. JASA.
