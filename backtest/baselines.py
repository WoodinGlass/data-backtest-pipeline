"""Baselines for M5. Contract: docs/adr/0014 §5.

Four baselines, all evaluated with the same test windows and same cost
model as the main model:

    b0_naive      p=0.5 always, no positions (classification only)
    b1_momentum   p=1 if px_return_20d > 0 else 0.45 (signal → risk/)
    b2_spy        SPY buy-and-hold (portfolio baseline, no risk stack)
    b3_top10      p=1 for top-10 by cross-sectional rank (signal → risk/)

Each baseline is a pure function:

    fn(features: pd.DataFrame, settings: BacktestSettings) -> pd.DataFrame

Output contract:
    Always contains columns: ticker, trade_date, prob
    Portfolio baselines ALSO contain: weight

The presence of the `weight` column tells the runner to skip the
risk/ stack (M4.5) and use the weights directly. Signal baselines
feed their probabilities through risk/entry -> risk/staking ->
risk/limits as usual.

No IO, no side effects, deterministic.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from backtest.config import BacktestSettings

# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


def _validate_features(
    features: pd.DataFrame,
    required_cols: list[str],
) -> None:
    base = {"ticker", "trade_date"}
    missing = [c for c in (list(base) + required_cols) if c not in features.columns]
    if missing:
        raise ValueError(f"features is missing required columns: {sorted(missing)}")


def _empty(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series([], dtype=object) for c in columns})


# ---------------------------------------------------------------------
# B0 — naive 50/50
# ---------------------------------------------------------------------


def b0_naive(
    features: pd.DataFrame,
    _settings: BacktestSettings,
) -> pd.DataFrame:
    """Naive: p = 0.5 for every row. No positions (cash)."""
    _validate_features(features, [])
    out = features[["ticker", "trade_date"]].copy().reset_index(drop=True)
    out["prob"] = 0.5
    out["weight"] = 0.0
    return out


# ---------------------------------------------------------------------
# B1 — momentum 20d
# ---------------------------------------------------------------------


def b1_momentum(
    features: pd.DataFrame,
    settings: BacktestSettings,
) -> pd.DataFrame:
    """p = 1.0 if trailing return > 0, else 0.45. Signal baseline."""
    col = f"px_return_{settings.momentum_lookback_days}d"
    _validate_features(features, [col])

    out = features[["ticker", "trade_date"]].copy().reset_index(drop=True)
    r = features[col].to_numpy(dtype=float)
    is_long = np.isfinite(r) & (r > 0.0)
    out["prob"] = np.where(is_long, 1.0, 0.45)
    return out


# ---------------------------------------------------------------------
# B2 — SPY buy-and-hold
# ---------------------------------------------------------------------


def b2_spy(
    features: pd.DataFrame,
    settings: BacktestSettings,
) -> pd.DataFrame:
    """Portfolio baseline: weight=1.0 on SPY for the entire window."""
    _validate_features(features, [])
    bench = settings.benchmark_ticker
    mask = features["ticker"] == bench
    out = features.loc[mask, ["ticker", "trade_date"]].copy().reset_index(drop=True)
    if out.empty:
        raise ValueError(f"b2_spy: benchmark ticker {bench!r} not found in features.")
    out["prob"] = 0.5
    out["weight"] = 1.0
    return out


# ---------------------------------------------------------------------
# B3 — always-long top-N by cross-sectional rank
# ---------------------------------------------------------------------

_TOP_N_RANK_COL = "cs_percentile_rank_universe_20d"


def b3_top_n(
    features: pd.DataFrame,
    settings: BacktestSettings,
) -> pd.DataFrame:
    """p = 1.0 for top-N by universe rank per date, else 0.45.

    Benchmark rows are excluded from selection. Ties broken by ticker.
    """
    _validate_features(features, [_TOP_N_RANK_COL])
    n = settings.top_n_baseline
    bench = settings.benchmark_ticker

    out = features[["ticker", "trade_date"]].copy().reset_index(drop=True)
    rank = features[_TOP_N_RANK_COL].to_numpy(dtype=float)
    is_bench = (features["ticker"] == bench).to_numpy()
    eligible = np.isfinite(rank) & ~is_bench

    # Compute descending rank within eligible rows, per trade_date.
    rnk = np.full(len(out), np.inf, dtype=float)
    if eligible.any():
        tmp = pd.DataFrame(
            {
                "trade_date": out["trade_date"].to_numpy(),
                "rank": rank,
                "eligible": eligible,
                "pos": np.arange(len(out)),
            }
        )
        elig = tmp.loc[tmp["eligible"]].copy()
        elig["rnk"] = elig.groupby("trade_date")["rank"].rank(method="first", ascending=False)
        rnk[elig["pos"].to_numpy()] = elig["rnk"].to_numpy()

    is_top = rnk <= n
    out["prob"] = np.where(is_top, 1.0, 0.45)
    return out


# ---------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------

_REGISTRY: dict[str, Callable[[pd.DataFrame, BacktestSettings], pd.DataFrame]] = {
    "b0_naive": b0_naive,
    "b1_momentum": b1_momentum,
    "b2_spy": b2_spy,
    "b3_top10": b3_top_n,
}

_PORTFOLIO_BASELINES: frozenset[str] = frozenset({"b0_naive", "b2_spy"})


def build_baseline(
    name: str,
    features: pd.DataFrame,
    settings: BacktestSettings | None = None,
) -> pd.DataFrame:
    """Dispatch to the requested baseline by name."""
    if settings is None:
        settings = BacktestSettings()
    if name not in _REGISTRY:
        raise ValueError(f"Unknown baseline: {name!r}. Available: {available_baselines()}.")
    return _REGISTRY[name](features, settings)


def is_portfolio_baseline(
    name_or_df: str | pd.DataFrame,
) -> bool:
    """True if the baseline bypasses the risk/ stack.

    Accepts either a baseline name or its output DataFrame. When a
    DataFrame is given, the check is column-based: presence of
    ``weight`` means portfolio-based.
    """
    if isinstance(name_or_df, str):
        return name_or_df in _PORTFOLIO_BASELINES
    return "weight" in name_or_df.columns


def available_baselines() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "available_baselines",
    "b0_naive",
    "b1_momentum",
    "b2_spy",
    "b3_top_n",
    "build_baseline",
    "is_portfolio_baseline",
]
