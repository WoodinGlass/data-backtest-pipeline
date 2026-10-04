# ADR 0013: Risk Framework

- **Status:** Accepted
- **Date:** 2026-10-04
- **Deciders:** Project maintainer
- **Related:** ADR 0008 (three-layer quality defense), ADR 0009 (macro data design), ADR 0010 (fundamental data design), ADR 0011 (memory management), ADR 0012 (feature engineering architecture)

## Context

M5 will run a walk-forward backtest against `fct_returns_daily` plus
the 23 point-in-time features built in M4. The model produces a
probability `p = P(return_{t+1} > 0)` per (ticker, trade_date).
However, a probability is not a position. Without an explicit
contract between *signal* and *position sizing*, three failure modes
appear:

1. **Implicit leverage.** A backtest that always uses full size
   silently assumes 1× NAV when the model is "sure" — but a noisy
   classifier is never sure. This overstates Sharpe.
2. **Untraceable parameters.** Magic numbers (`0.05`, `-0.10`)
   scattered across the backtest make sensitivity analysis and code
   review impossible.
3. **Every model iteration triggers a backtest refactor.** Without a
   versioned contract, changing the staking rule means rewriting the
   simulator.

This ADR locks the risk framework for v1 so that M5 can be written
once, cleanly, against a stable interface.

## Decision

We adopt a **pluggable risk framework** with eight locked decisions.
Every parameter lives in `risk/config.py` as `RiskSettings`
(pydantic-settings) and can be overridden via `DBP_RISK_*` env vars —
**never** hardcoded in the backtest.

### 1. Staking: Fractional Kelly (Quarter-Kelly)

```text
f* = (b*p - q) / b          where b = 1, q = 1 - p
   = 2p - 1                 (even-odds binary, long-only)
f  = min(k * f*, cap)       k = 0.25, cap = 0.05
f  = max(f, 0)              long+flat, clip negatives
```

- **`k = 0.25`** (quarter-Kelly). Full Kelly is optimal only if `p`
  is exactly the true probability. Our model is **not** that
  accurate; a classifier with AUC ~0.53 has calibration error of
  several percentage points. Quarter-Kelly tolerates estimation
  error of roughly 2× before losing to equal-weight.
- **`cap = 0.05`** (5% NAV per name). Prevents concentration when the
  model outputs extreme `p`. With 31 names, equal weight is 3.2%;
  a 5% cap allows modest overweight without single-name blow-up.
- **Negative `f` clipped to 0.** v1 is long+flat (see §4).

**Alternative staking rules** exposed via registry for M5 sweeps:
`kelly`, `fixed_fractional`, `equal_weight`, `vol_target`.

### 2. Volatility targeting

```text
scale = min(1.0, target_vol / realized_vol_20d)
size  = f * scale
```

- **`target_vol = 0.10`** annualized, portfolio-level.
- **`lookback = 20`** trading days.
  `realized_vol = std(log_ret_20d) * sqrt(252)`.
- **`min(1.0, ...)`** — scale only shrinks. No leverage-up in calm
  regimes. This is a deliberate conservatism: leverage-up is where
  backtests quietly lie.

### 3. Stop loss and drawdown halt

| Trigger | Action | Rationale |
|---|---|---|
| Position return ≤ **-8%** | Exit position, block re-entry 5d | ≈ 2.5 ATR for large-cap |
| Portfolio DD ≤ **-10%** | De-risk 50% (all positions ×0.5) | Prevent death spiral |
| Portfolio DD ≤ **-20%** | Halt trading for the rest of the run | Classic DD limit |

Drawdown is measured peak-to-trough NAV **within the backtest window**
(not cumulative across resets). Cooldown after stop-out prevents
immediate re-entry into a broken position.


**Interaction between derisk and halt.** Once derisk reduces
exposure, portfolio drawdown grows more slowly. In a typical
scenario the halt trigger (`-20%`) is therefore **unreachable** —
derisk is the designed "soft" circuit breaker, halt is the "hard"
one, and the two are not independent. This is intentional: a
portfolio that has already halved exposure should not also freeze
completely on the same price path. If a halt must be reachable
regardless of derisk, set `dd_derisk_factor = 1.0` (derisk becomes
a no-op) — this is the configuration used by
`tests/unit/test_risk_limits.py::TestDrawdownHalt`.

### 4. Direction: long + flat

- `p > entry_threshold` (default `0.55`) → candidate long.
- `p ≤ entry_threshold` → **flat** (not short).
- Idle cash earns Fed Funds (see §5).
- Borrow cost model is **not required** in v1.

Long-short is deferred to M5.5 with its own ADR (borrow model, short
squeeze handling, hard-to-borrow list).

### 5. Idle cash: Fed Funds rate

- Idle cash compounds at `mc_fedfunds` from `marts.fct_macro_daily`.
- `daily_rate = (1 + annual_rate) ** (1/252) - 1`.
- If `mc_fedfunds` is NULL on a given `trade_date` → **0%**
  (fail-safe, never fabricate a rate).
- **Benchmark remains SPY buy-and-hold.** Idle cash is not a
  performance benchmark; it is a modeling choice so the backtest
  reflects a real portfolio.

### 6. Transaction costs

| Component | Value | Round-trip |
|---|---|---|
| Commission | 0.5 bp | 1.0 bp |
| Slippage | 2.0 bp | 4.0 bp |
| **One-way total** | **2.5 bp** | — |
| **Round-trip total** | — | **5.0 bp** |
| Borrow (short, M5.5) | 50 bp annualized | — |

Cost is charged whenever `position_t != position_{t-1}`:

```text
cost_t = Σ_i |Δweight_{i,t}| * NAV_t * one_way_cost_bp / 10_000
```

The cost model is **reversible** — M5 must include sensitivity
analysis at 0 bp and 5 bp to bound the impact.

### 7. Rebalance: threshold-based

- Signal is computed **every day**.
- A trade fires only when `|target_weight - current_weight| > 1% NAV`
  for at least one name.
- `current_weight` is updated after each trade and drifts with price.
- Rebalance uses only `t`-dated information: `p_t`, `NAV_t`,
  `current_positions_t`. No future data.

### 8. Registry pattern

All components are accessed through registries so M5 can sweep
parameters without touching the backtest loop:

```python
STAKING = {"kelly": ..., "fixed_fractional": ..., "equal_weight": ..., "vol_target": ...}
ENTRY   = {"threshold": ..., "top_n": ..., "cross_sectional": ...}
LIMITS  = {"stop_loss": ..., "dd_halt": ...}
COST    = {"bp_model": ...}
```

Selection is via `RiskSettings.staking_method`, `entry_method`, etc.
No `if/elif` chains in the backtest.

## Consequences

### Positive

- Backtest parameter sweeps require no code changes — only env vars
  or a `RiskSettings` override.
- Every number in the M5 report is traceable to this ADR plus a
  commit hash.
- Non-leakage is guaranteed by construction: the entry rule only sees
  `p_t`, `NAV_t`, `positions_t`.
- Quarter-Kelly + vol targeting + DD halt together bound worst-case
  portfolio loss, which is what a real risk framework must do.

### Negative

- Quarter-Kelly will produce **lower Sharpe** than a tuned full-Kelly
  backtest. This is intentional: we prefer an honest lower number to
  an inflated higher one.
- Fixed 5 bp round-trip cost may be high for zero-commission brokers.
  Mitigation: sensitivity analysis at 0 bp in M5.
- Three DD thresholds are more parameters to tune. Mitigation: they
  are locked in this ADR; changes require an ADR amendment.

### Neutral

- Long-short, leverage, options, and multi-asset are out of scope for
  v1.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Full Kelly | Variance explosion with a noisy `p`; can produce >100% NAV positions |
| Equal weight (1/N) | Ignores signal strength entirely |
| Always long SPY | Not a strategy — it is the benchmark |
| Long-short from day one | Requires borrow model, hard-to-borrow list, short-squeeze handling |
| Cash idle at 0% | Understates the portfolio; Fed Funds is available and PIT-correct |
| Per-name vol targeting | Noisier at single-name level; portfolio-level is more stable |
| Daily rebalance | Excessive churn; 1% threshold captures most of the signal |

## References

- Kelly, J. L. (1956). *A New Interpretation of Information Rate*.
  Bell System Technical Journal.
- MacLean, L. C., Thorp, E. O., & Ziemba, W. T. (2011). *The Kelly
  Capital Growth Criterion: Theory and Practice*. World Scientific.
- López de Prado, M. (2018). *Advances in Financial Machine Learning*.
  Wiley. Chapter 10 (Bet Sizing).
- Chan, E. P. (2013). *Algorithmic Trading: Winning Strategies and
  Their Rationale*. Wiley. Chapter 6 (position sizing).
- Harvey, C. R., & Liu, Y. (2015). *Backtesting*. Journal of Portfolio
  Management — on why unstated assumptions (costs, sizing) inflate
  reported Sharpe.
