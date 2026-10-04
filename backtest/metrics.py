"""Metrics for M5 — pure functions, no IO.

Contract: docs/adr/0014-walk-forward-methodology.md §6.

Three groups:
    classification    log loss, Brier, AUC, hit rate, calibration
    trading           Sharpe, CAGR, MDD, turnover, cost-adj return
    advanced          bootstrap CI, deflated Sharpe

Every function:
    - takes only inputs, returns a scalar / tuple / dict
    - never mutates its arguments
    - handles edge cases (empty input, zero variance) without raising

Numbers are annualized at 252 trading days unless stated otherwise.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import norm
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    brier_score_loss as sk_brier,
)
from sklearn.metrics import (
    log_loss as sk_log_loss,
)
from sklearn.metrics import (
    roc_auc_score as sk_auc,
)

# =====================================================================
# Classification metrics
# =====================================================================


def log_loss(y_true: np.ndarray, p: np.ndarray) -> float:
    """Log loss (cross-entropy). sklearn wrapper for parity."""
    y_true = np.asarray(y_true)
    p = np.asarray(p, dtype=float)
    if y_true.size == 0:
        return float("nan")
    if len(np.unique(y_true)) < 2:
        # sklearn raises if only one class present
        return float("nan")
    return float(sk_log_loss(y_true, np.clip(p, 1e-15, 1 - 1e-15)))


def brier(y_true: np.ndarray, p: np.ndarray) -> float:
    """Brier score = mean((p - y)^2). Lower is better."""
    y_true = np.asarray(y_true)
    p = np.asarray(p, dtype=float)
    if y_true.size == 0:
        return float("nan")
    return float(sk_brier(y_true, p))


def auc(y_true: np.ndarray, p: np.ndarray) -> float:
    """ROC AUC. 0.5 = random, 1.0 = perfect."""
    y_true = np.asarray(y_true)
    p = np.asarray(p, dtype=float)
    if y_true.size == 0 or len(np.unique(y_true)) < 2:
        return float("nan")
    return float(sk_auc(y_true, p))


def hit_rate(y_true: np.ndarray, p: np.ndarray) -> float:
    """Fraction of predictions where sign(p - 0.5) matches y."""
    y_true = np.asarray(y_true)
    p = np.asarray(p, dtype=float)
    if y_true.size == 0:
        return float("nan")
    y_hat = (p > 0.5).astype(int)
    return float((y_hat == y_true).mean())


def calibration_slope_intercept(
    y_true: np.ndarray,
    p: np.ndarray,
) -> tuple[float, float]:
    """Regress y on logit(p). Perfect calibration -> slope=1, intercept=0.

    Returns (slope, intercept). NaN if fit fails (e.g. one class only).
    """
    y_true = np.asarray(y_true)
    p = np.asarray(p, dtype=float)
    if y_true.size < 2 or len(np.unique(y_true)) < 2:
        return (float("nan"), float("nan"))
    # Clip to avoid logit(0) / logit(1)
    eps = 1e-6
    p_clip = np.clip(p, eps, 1 - eps)
    z = np.log(p_clip / (1 - p_clip))
    try:
        lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=1000)
        lr.fit(z.reshape(-1, 1), y_true)
        return (float(lr.coef_[0, 0]), float(lr.intercept_[0]))
    except Exception:
        return (float("nan"), float("nan"))


def classification_metrics(
    y_true: np.ndarray,
    p: np.ndarray,
) -> dict[str, float]:
    """Bundle all classification metrics for one fold."""
    slope, intercept = calibration_slope_intercept(y_true, p)
    return {
        "log_loss": log_loss(y_true, p),
        "brier": brier(y_true, p),
        "auc": auc(y_true, p),
        "hit_rate": hit_rate(y_true, p),
        "calibration_slope": slope,
        "calibration_intercept": intercept,
        "n_obs": len(y_true),
    }


# =====================================================================
# Trading metrics
# =====================================================================


def sharpe(returns: np.ndarray, annualization: int = 252) -> float:
    """Annualized Sharpe: mean/std * sqrt(annualization). 0 if std = 0."""
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return float("nan")
    mu = float(np.nanmean(r))
    sd = float(np.nanstd(r, ddof=1))
    # Use a tolerance, not equality: for near-constant float
    # series, np.std may be ~1e-19 instead of exactly 0.
    if sd < 1e-12 or not np.isfinite(sd):
        return 0.0
    return mu / sd * np.sqrt(annualization)


def cagr(returns: np.ndarray, annualization: int = 252) -> float:
    """Compound annual growth rate. -1 if total return <= -100%."""
    r = np.asarray(returns, dtype=float)
    if r.size == 0:
        return float("nan")
    total = float(np.prod(1.0 + r))
    if total <= 0:
        return -1.0
    n_years = r.size / annualization
    if n_years <= 0:
        return float("nan")
    return total ** (1.0 / n_years) - 1.0


def max_drawdown(returns: np.ndarray) -> float:
    """Max peak-to-trough drawdown as a positive number (e.g. 0.20)."""
    r = np.asarray(returns, dtype=float)
    if r.size == 0:
        return float("nan")
    cum = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(cum)
    dd = 1.0 - cum / peak
    return float(np.nanmax(dd))


def turnover_per_date(weights_wide: pd.DataFrame) -> pd.Series:
    """sum_i |w_{i,t} - w_{i,t-1}| per date. Two-sided turnover."""
    if weights_wide.empty:
        return pd.Series(dtype=float)
    delta = weights_wide.diff().abs().sum(axis=1)
    # first row has no prior, set to sum of absolute weights (i.e. from 0)
    delta.iloc[0] = weights_wide.iloc[0].abs().sum()
    return delta


def cost_adjusted_return(
    gross_returns: np.ndarray,
    turnover: np.ndarray,
    one_way_cost_bp: float,
) -> np.ndarray:
    """Subtract cost = turnover * one_way_cost_bp / 10_000 from each day."""
    g = np.asarray(gross_returns, dtype=float)
    t = np.asarray(turnover, dtype=float)
    return g - t * one_way_cost_bp / 10_000.0


def trading_metrics(
    returns: np.ndarray,
    weights_wide: pd.DataFrame | None = None,
    one_way_cost_bp: float = 0.0,
    annualization: int = 252,
) -> dict[str, float]:
    """Bundle trading metrics. Cost adjusted if weights_wide provided.

    - `returns` is net of any cost already.
    - `weights_wide` (optional): index=trade_date, cols=ticker.
      If given, turnover is computed and reported.
    """
    r = np.asarray(returns, dtype=float)
    out: dict[str, float] = {
        "sharpe": sharpe(r, annualization),
        "cagr": cagr(r, annualization),
        "max_drawdown": max_drawdown(r),
        "total_return": float(np.prod(1.0 + r) - 1.0) if r.size else float("nan"),
        "n_days": int(r.size),
    }
    if weights_wide is not None and not weights_wide.empty:
        t = turnover_per_date(weights_wide).values
        out["avg_daily_turnover"] = float(np.mean(t))
        out["annual_turnover"] = float(np.mean(t) * annualization)
        out["total_cost_bp"] = float(np.sum(t) * one_way_cost_bp)
    return out


# =====================================================================
# Advanced: bootstrap CI + deflated Sharpe
# =====================================================================


def bootstrap_sharpe_ci(
    returns: np.ndarray,
    n_resamples: int = 1000,
    ci: float = 0.95,
    seed: int = 42,
    block: int = 1,
) -> tuple[float, float, float]:
    """Block bootstrap CI for Sharpe.

    Block=1 resamples individual days. Block>1 uses contiguous blocks
    of size `block` to preserve autocorrelation. For daily returns we
    recommend block=5 (one trading week).

    Returns (point_estimate, lower, upper).
    """
    r = np.asarray(returns, dtype=float)
    if r.size < 5:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    point = sharpe(r)
    n = r.size
    if block <= 1:
        samples = np.empty(n_resamples)
        for i in range(n_resamples):
            idx = rng.integers(0, n, size=n)
            samples[i] = sharpe(r[idx])
    else:
        # Contiguous block bootstrap
        n_blocks = int(np.ceil(n / block))
        max_start = n - block
        if max_start < 1:
            return (point, float("nan"), float("nan"))
        samples = np.empty(n_resamples)
        for i in range(n_resamples):
            starts = rng.integers(0, max_start + 1, size=n_blocks)
            idx = np.concatenate([np.arange(s, s + block) for s in starts])[:n]
            samples[i] = sharpe(r[idx])
    lo = float(np.nanpercentile(samples, (1 - ci) / 2 * 100))
    hi = float(np.nanpercentile(samples, (1 + ci) / 2 * 100))
    return (point, lo, hi)


def deflated_sharpe(
    observed_sharpe: float,
    n_trials: int,
    n_obs: int,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> float:
    """Deflated Sharpe ratio (Bailey & Lopez de Prado, 2014).

    Returns the probability that the true Sharpe exceeds the expected
    maximum Sharpe under the null of N independent trials with the
    same sample size. Values > 0.95 mean the observed Sharpe is very
    unlikely to be a fluke.

    Inputs:
        observed_sharpe   annualized or per-period; must match the units
                          used to derive n_obs (per-period is typical).
        n_trials          effective number of strategy variants tried.
        n_obs             number of observations used to estimate Sharpe.
        skew, kurt        skewness and (non-excess) kurtosis of returns.

    Reference:
        Bailey, D. H., & Lopez de Prado, M. (2014). The Deflated Sharpe
        Ratio. Journal of Portfolio Management, 40(5).
    """
    if n_trials < 1 or n_obs < 2 or not np.isfinite(observed_sharpe):
        return float("nan")

    euler_gamma = 0.5772156649015329
    # Expected max of N standard normals (Bailey & Lopez de Prado eq. 8)
    # Using the standard approximation
    z1 = norm.ppf(1.0 - 1.0 / n_trials)
    z2 = norm.ppf(1.0 - 1.0 / (n_trials * np.e))
    expected_max = np.sqrt(1.0 / (n_obs - 1)) * ((1.0 - euler_gamma) * z1 + euler_gamma * z2)

    # Variance of the Sharpe estimator under non-normality
    # (Bailey & Lopez de Prado eq. 9)
    var_term = (1.0 - skew * observed_sharpe + (kurt - 1.0) / 4.0 * observed_sharpe**2) / (
        n_obs - 1
    )
    if var_term <= 0:
        return float("nan")
    std_sr = np.sqrt(var_term)

    # Deflated Sharpe = P(SR > expected_max | observed)
    z = (observed_sharpe - expected_max) / std_sr
    return float(norm.cdf(z))


def realized_skew_kurt(returns: np.ndarray) -> tuple[float, float]:
    """Sample skewness and (non-excess) kurtosis of returns."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 4:
        return (0.0, 3.0)
    mu = r.mean()
    sd = r.std(ddof=1)
    if sd < 1e-12:
        return (0.0, 3.0)
    z = (r - mu) / sd
    skew = float((z**3).mean())
    kurt = float((z**4).mean())
    return (skew, kurt)


__all__ = [  # noqa: RUF022 (grouped by category, not alphabetical)
    # classification
    "auc",
    "brier",
    "calibration_slope_intercept",
    "classification_metrics",
    "hit_rate",
    "log_loss",
    # trading
    "cagr",
    "cost_adjusted_return",
    "max_drawdown",
    "sharpe",
    "trading_metrics",
    "turnover_per_date",
    # advanced
    "bootstrap_sharpe_ci",
    "deflated_sharpe",
    "realized_skew_kurt",
]
