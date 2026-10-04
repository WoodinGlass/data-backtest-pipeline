# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (M5 — Walk-forward backtest)

- **ADR 0014** — walk-forward methodology (rolling 36m/3m/3m, purge +
  embargo, logistic v1, time-decay weights, per-fold + pooled +
  yearly reporting).
- `backtest/config.py` — `BacktestSettings` (27 fields, 3 cross-field
  validators, `DBP_BT_*` env prefix).
- `backtest/split.py` — fold generator with purge (1 row) + embargo
  (5 days), test windows non-overlapping.
- `backtest/metrics.py` — classification (log loss, Brier, AUC, hit
  rate, calibration), trading (Sharpe, CAGR, MDD, turnover,
  cost-adjusted), advanced (bootstrap CI, deflated Sharpe).
- `backtest/baselines.py` — B0 naive, B1 momentum 20d, B2 SPY
  buy-and-hold, B3 always-long top-10 (registry pattern).
- `backtest/model.py` — logistic regression with time-decay sample
  weights (half-life 252d), median imputation, single-class fallback.
- `backtest/portfolio.py` — `risk/` integration, rebalance band
  (1% NAV), Fed Funds idle cash, cost model.
- `backtest/runner.py` — per-fold orchestration, reproducibility
  (global seed), persisted artifacts (predictions, returns, metrics).
- `backtest/report.py` — aggregate per fold + pooled + yearly,
  bootstrap CI, deflated Sharpe, calibration + equity curve plots.
- `scripts/run_backtest.py` — CLI end-to-end (features + warehouse
  + cash rates).
- ~130 unit tests in `tests/unit/test_backtest_*.py`.

### Fixed (M5 discovered)

- **`risk/entry.py::_select_threshold` did not exclude benchmark
  rows.** Unlike `top_n` and `cross_sectional`, the threshold rule
  only compared `prob > threshold`; SPY could be selected if its
  prob exceeded the threshold, violating ADR 0013 §4. Latent from
  M4.5 (M4.5 fixture had SPY.prob=0.50 < threshold=0.55 by
  coincidence). Found by the M5 integration test. Fixed by reusing
  `_eligible_mask`; regression test added.

### Changed

- `docs/adr/` count is now 0001–0014.

