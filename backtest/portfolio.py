"""Portfolio layer for M5. Contract: ADR 0014 §7.

Consumes predictions, runs them through the locked risk framework
(ADR 0013), applies a rebalance band, and returns daily portfolio
returns with cost.

Public API:

    run_portfolio(predictions, prices, next_returns, cash_rates,
                  risk_settings, bt_settings) -> PortfolioResult

    run_portfolio_from_weights(weights_target, next_returns,
                               cash_rates, risk_settings,
                               bt_settings) -> PortfolioResult

The second entrypoint is for portfolio baselines (B0, B2) that
provide weights directly and bypass the risk stack.

Timing convention (matches ADR 0014 and the M4 feature PIT contract):

- Features at trade date t are known at close of t.
- The label `next_return` at t is the return from t to t+1.
- Weights committed at end of day t earn next_return at t.
- Portfolio return on date t is attributed to the end of day t.
- Costs are charged at the moment of committing a new weight.

Pure: neither function mutates its inputs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtest.config import BacktestSettings
from risk.config import RiskSettings
from risk.entry import apply_entry_rules
from risk.limits import apply_all_limits
from risk.staking import compute_weights

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioResult:
    """Output of a portfolio simulation."""

    returns: pd.DataFrame  # cols: gross, cost, net, cash_ret; index=trade_date
    weights: pd.DataFrame  # index=trade_date, cols=ticker, committed EOD
    turnover: pd.Series  # index=trade_date, one-sided turnover
    n_positions: pd.Series  # index=trade_date, count of non-zero weights
    n_days: int
    cash_weight: pd.Series  # index=trade_date, 1 - sum(weights)


# ---------------------------------------------------------------------
# Internal: pivot helpers
# ---------------------------------------------------------------------


def _pivot_weights(df: pd.DataFrame) -> pd.DataFrame:
    """long (ticker, trade_date, weight) -> wide index=trade_date, cols=ticker."""
    if df.empty:
        return pd.DataFrame()
    return df.pivot(index="trade_date", columns="ticker", values="weight").sort_index().fillna(0.0)


def _pivot_returns(df: pd.DataFrame) -> pd.DataFrame:
    """long (ticker, trade_date, next_return) -> wide."""
    if df.empty:
        return pd.DataFrame()
    return df.pivot(index="trade_date", columns="ticker", values="next_return").sort_index()


def _align_columns(a: pd.DataFrame, b: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Align b's columns to a's columns, filling missing with 0."""
    cols = a.columns
    b = b.reindex(columns=cols)
    return a, b


# ---------------------------------------------------------------------
# Rebalance band
# ---------------------------------------------------------------------


def apply_rebalance_band(
    target_weights: pd.DataFrame,
    next_returns: pd.DataFrame,
    threshold: float,
    cash_annual_rates: pd.Series | None = None,  # noqa: ARG001
    annualization: int = 252,  # noqa: ARG001
) -> tuple[pd.DataFrame, pd.Series]:
    """Chronological walk applying a per-name rebalance band.

    At each date t:
        1. Apply last-committed weights to next_return_{t-1} to
           compute portfolio return from t-1 to t.
        2. Drift each name's weight by its own return.
        3. Compare drifted weight to target; commit target if the
           absolute difference exceeds ``threshold``, else keep the
           drifted weight.

    Returns (committed_weights_wide, turnover_series).
    """
    if target_weights.empty:
        return target_weights.copy(), pd.Series(dtype=float)

    target = target_weights.sort_index()
    rets = next_returns.reindex(
        index=target.index,
        columns=target.columns,
    )
    dates = list(target.index)
    cols = list(target.columns)

    committed = pd.DataFrame(0.0, index=dates, columns=cols)
    turnover = pd.Series(0.0, index=dates, dtype=float)

    # First date: no prior position. Fully take target.
    committed.iloc[0] = target.iloc[0].values
    turnover.iloc[0] = float(np.abs(target.iloc[0]).sum())

    for i in range(1, len(dates)):
        d_prev = dates[i - 1]
        # Return applied to positions held over (t-1, t): next_return at t-1
        r = rets.loc[d_prev].fillna(0.0).to_numpy()
        w_prev = committed.loc[d_prev].to_numpy()
        # Portfolio return from t-1 to t (before new trades at t)
        port_ret = float((w_prev * r).sum())
        # Drift weights by realized returns (renormalize by (1+port_ret))
        drifted = w_prev * (1.0 + r) / (1.0 + port_ret) if port_ret > -0.999999 else w_prev * 0.0

        target_t = target.loc[dates[i]].to_numpy()
        # Per-name band decision
        delta = np.abs(target_t - drifted)
        commit_target = delta > threshold
        w_new = np.where(commit_target, target_t, drifted)
        committed.iloc[i] = w_new
        # Turnover = |committed_new - drifted_old|
        turnover.iloc[i] = float(np.abs(w_new - drifted).sum())

    # Turnover first date already set; recompute first-date trade is
    # |target_0 - 0| = |target_0|, which matches the initial commit.
    return committed, turnover


# ---------------------------------------------------------------------
# Portfolio returns
# ---------------------------------------------------------------------


def compute_portfolio_returns(
    committed_weights: pd.DataFrame,
    next_returns: pd.DataFrame,
    cash_annual_rates: pd.Series | None,
    one_way_cost_bp: float,
    turnover: pd.Series,
    annualization: int = 252,
) -> pd.DataFrame:
    """Given committed weights and next returns, produce daily returns.

    Returns DataFrame with columns: gross, cost, net, cash_ret.
    """
    if committed_weights.empty:
        return pd.DataFrame(
            columns=["gross", "cost", "net", "cash_ret"],
            dtype=float,
        )

    w = committed_weights.sort_index()
    r = next_returns.reindex(
        index=w.index,
        columns=w.columns,
    ).fillna(0.0)

    # Gross return from weights at t applied to next_return at t
    gross = (w * r).sum(axis=1)

    # Cash: residual weight earns risk-free (converted from annual)
    cash_w = 1.0 - w.sum(axis=1)
    if cash_annual_rates is None:
        cash_ret = pd.Series(0.0, index=w.index)
    else:
        ann = cash_annual_rates.reindex(w.index).fillna(0.0)
        # Convert annualized to daily compounding (geometric)
        daily = (1.0 + ann) ** (1.0 / annualization) - 1.0
        daily = daily.where(np.isfinite(daily), 0.0)
        cash_ret = cash_w * daily

    # Cost charged on turnover
    cost = turnover.reindex(w.index).fillna(0.0) * (one_way_cost_bp / 10_000.0)

    net = gross + cash_ret - cost

    return pd.DataFrame(
        {
            "gross": gross,
            "cost": cost,
            "net": net,
            "cash_ret": cash_ret,
        },
        index=w.index,
    )


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------


def run_portfolio_from_weights(
    target_weights: pd.DataFrame,
    next_returns: pd.DataFrame,
    risk_settings: RiskSettings | None = None,
    bt_settings: BacktestSettings | None = None,
    cash_annual_rates: pd.Series | None = None,
) -> PortfolioResult:
    """For portfolio baselines (B0, B2) that provide weights directly.

    ``target_weights`` is wide: index=trade_date, cols=ticker.
    ``next_returns`` is wide: same shape.
    """
    if risk_settings is None:
        risk_settings = RiskSettings()
    if bt_settings is None:
        bt_settings = BacktestSettings()

    if target_weights.empty or next_returns.empty:
        return PortfolioResult(
            returns=pd.DataFrame(columns=["gross", "cost", "net", "cash_ret"]),
            weights=pd.DataFrame(),
            turnover=pd.Series(dtype=float),
            n_positions=pd.Series(dtype=int),
            n_days=0,
            cash_weight=pd.Series(dtype=float),
        )

    aligned_w, aligned_r = _align_columns(target_weights, next_returns)

    committed, turnover = apply_rebalance_band(
        aligned_w,
        aligned_r,
        threshold=risk_settings.rebalance_threshold,
        cash_annual_rates=cash_annual_rates,
    )

    returns = compute_portfolio_returns(
        committed,
        aligned_r,
        cash_annual_rates=cash_annual_rates,
        one_way_cost_bp=risk_settings.one_way_cost_bp,
        turnover=turnover,
    )

    n_pos = (committed.abs() > 1e-12).sum(axis=1).astype(int)
    cash_w = 1.0 - committed.sum(axis=1)

    return PortfolioResult(
        returns=returns,
        weights=committed,
        turnover=turnover,
        n_positions=n_pos,
        n_days=len(committed),
        cash_weight=cash_w,
    )


def run_portfolio(
    predictions: pd.DataFrame,
    prices: pd.DataFrame,
    next_returns: pd.DataFrame,
    risk_settings: RiskSettings | None = None,
    bt_settings: BacktestSettings | None = None,
    cash_annual_rates: pd.Series | None = None,
) -> PortfolioResult:
    """Full pipeline: predictions -> risk/ -> rebalance -> daily returns.

    Parameters
    ----------
    predictions : long format
        Columns: ticker, trade_date, prob. Optional: is_benchmark.
    prices : long format
        Columns: ticker, trade_date, close. Used by risk/limits.
    next_returns : long format
        Columns: ticker, trade_date, next_return. Realized return from
        trade_date to trade_date + 1.
    risk_settings, bt_settings
        Configuration objects. Defaults used if None.
    cash_annual_rates : Series, optional
        Index=trade_date, values=annualized rate (e.g. 0.05 = 5%).
    """
    if risk_settings is None:
        risk_settings = RiskSettings()
    if bt_settings is None:
        bt_settings = BacktestSettings()

    # 1. Entry
    entered = apply_entry_rules(predictions, risk_settings)
    selected = entered[entered["selected"]].copy()

    if selected.empty:
        empty_wide = pd.DataFrame()
        return PortfolioResult(
            returns=pd.DataFrame(columns=["gross", "cost", "net", "cash_ret"]),
            weights=empty_wide,
            turnover=pd.Series(dtype=float),
            n_positions=pd.Series(dtype=int),
            n_days=0,
            cash_weight=pd.Series(dtype=float),
        )

    # 2. Staking (returns Series aligned to selected.index)
    weights_series = compute_weights(selected, risk_settings)
    selected["weight"] = weights_series.values

    # 3. Limits
    weights_in = selected[["ticker", "trade_date", "weight"]].copy()
    limited = apply_all_limits(weights_in, prices, risk_settings)

    # 4. Wide pivots
    target_wide = _pivot_weights(limited)
    nr_wide = _pivot_returns(next_returns)

    # 5. Align and rebalance
    target_wide, nr_wide = _align_columns(target_wide, nr_wide)

    committed, turnover = apply_rebalance_band(
        target_wide,
        nr_wide,
        threshold=risk_settings.rebalance_threshold,
        cash_annual_rates=cash_annual_rates,
    )

    returns = compute_portfolio_returns(
        committed,
        nr_wide,
        cash_annual_rates=cash_annual_rates,
        one_way_cost_bp=risk_settings.one_way_cost_bp,
        turnover=turnover,
    )

    n_pos = (committed.abs() > 1e-12).sum(axis=1).astype(int)
    cash_w = 1.0 - committed.sum(axis=1)

    return PortfolioResult(
        returns=returns,
        weights=committed,
        turnover=turnover,
        n_positions=n_pos,
        n_days=len(committed),
        cash_weight=cash_w,
    )


__all__ = [
    "PortfolioResult",
    "apply_rebalance_band",
    "compute_portfolio_returns",
    "run_portfolio",
    "run_portfolio_from_weights",
]
