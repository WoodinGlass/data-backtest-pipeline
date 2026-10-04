"""Report aggregation for M5. Contract: ADR 0014 §6.

Public API:

    aggregate_fold_metrics(result) -> pd.DataFrame
        Per-model, per-fold summary (classification + trading).

    summarize(result) -> dict
        Aggregated numbers: median+IQR, mean+std per model;
        pooled Sharpe/AUC; yearly Sharpe; bootstrap CI; deflated
        Sharpe. This is the object that gets serialized.

    build_report(result, output_dir) -> Path
        Write summary.json, summary tables, and (if matplotlib is
        available) calibration + equity curves.

Input: RunResult from backtest.runner.
No mutation of inputs. Deterministic given the same RunResult.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest.metrics import (
    bootstrap_sharpe_ci,
    classification_metrics,
    deflated_sharpe,
    realized_skew_kurt,
    sharpe,
    trading_metrics,
)
from backtest.runner import FoldResult, RunResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Per-fold aggregation
# ---------------------------------------------------------------------

_CLS_KEYS = (
    "log_loss",
    "brier",
    "auc",
    "hit_rate",
    "calibration_slope",
    "calibration_intercept",
)
_TRD_KEYS = (
    "sharpe",
    "cagr",
    "max_drawdown",
    "total_return",
    "avg_daily_turnover",
    "annual_turnover",
    "total_cost_bp",
)


def _fold_rows(model: str, fr: FoldResult) -> dict[str, Any]:
    row: dict[str, Any] = {
        "model": model,
        "fold_id": fr.fold_id,
        "test_start": fr.test_start,
        "test_end": fr.test_end,
    }
    for k in _CLS_KEYS:
        row[f"cls_{k}"] = fr.classification.get(k, float("nan"))
    for k in _TRD_KEYS:
        row[f"trd_{k}"] = fr.trading.get(k, float("nan"))
    return row


def aggregate_fold_metrics(result: RunResult) -> pd.DataFrame:
    """One row per (model, fold)."""
    rows: list[dict[str, Any]] = []
    for fr in result.main:
        rows.append(_fold_rows("main", fr))
    for name, br in result.baselines.items():
        for fr in br.fold_results:
            rows.append(_fold_rows(name, fr))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------
# Statistical aggregation across folds
# ---------------------------------------------------------------------


def _median_iqr(values: Iterable[float]) -> dict[str, float]:
    arr = np.asarray(list(values), dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {
            "median": float("nan"),
            "q25": float("nan"),
            "q75": float("nan"),
            "iqr": float("nan"),
            "n": 0,
        }
    q25, median, q75 = np.percentile(arr, [25, 50, 75])
    return {
        "median": float(median),
        "q25": float(q25),
        "q75": float(q75),
        "iqr": float(q75 - q25),
        "n": int(arr.size),
    }


def _mean_std(values: Iterable[float]) -> dict[str, float]:
    arr = np.asarray(list(values), dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return {"mean": float("nan"), "std": float("nan"), "n": 0}
    return {
        "mean": float(arr.mean()),
        "std": float(arr.std(ddof=1)) if arr.size > 1 else 0.0,
        "n": int(arr.size),
    }


def _aggregate_model(fold_results: list[FoldResult]) -> dict[str, Any]:
    """Median+IQR and mean+std for classification and trading keys."""
    agg: dict[str, Any] = {"n_folds": len(fold_results)}
    for k in _CLS_KEYS:
        vals = [fr.classification.get(k) for fr in fold_results]
        agg[f"cls_{k}_median_iqr"] = _median_iqr(vals)
        agg[f"cls_{k}_mean_std"] = _mean_std(vals)
    for k in _TRD_KEYS:
        vals = [fr.trading.get(k) for fr in fold_results]
        agg[f"trd_{k}_median_iqr"] = _median_iqr(vals)
        agg[f"trd_{k}_mean_std"] = _mean_std(vals)
    return agg


# ---------------------------------------------------------------------
# Pooled metrics (concatenate predictions and returns)
# ---------------------------------------------------------------------


def _concat_predictions(fold_results: list[FoldResult]) -> pd.DataFrame:
    if not fold_results:
        return pd.DataFrame()
    parts = []
    for fr in fold_results:
        if fr.predictions.empty:
            continue
        p = fr.predictions.copy()
        p["fold_id"] = fr.fold_id
        parts.append(p)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def _concat_net_returns(fold_results: list[FoldResult]) -> pd.Series:
    if not fold_results:
        return pd.Series(dtype=float)
    parts = []
    for fr in fold_results:
        if fr.returns.empty:
            continue
        r = fr.returns["net"].copy()
        r.name = fr.fold_id
        parts.append(r)
    if not parts:
        return pd.Series(dtype=float)
    # Keep as series indexed by trade_date (may collide across folds,
    # but here folds do not overlap by construction).
    out = pd.concat(parts)
    out = out[~out.index.duplicated(keep="first")]
    return out.sort_index()


def _pooled_metrics(
    fold_results: list[FoldResult],
    n_resamples: int,
    ci: float,
    seed: int,
) -> dict[str, Any]:
    preds = _concat_predictions(fold_results)
    rets = _concat_net_returns(fold_results)

    out: dict[str, Any] = {"n_obs": len(preds), "n_days": len(rets)}

    if not preds.empty:
        y = preds["y_true"].to_numpy()
        p = preds["prob"].to_numpy()
        # Drop NaNs
        ok = np.isfinite(y) & np.isfinite(p)
        y, p = y[ok].astype(int), p[ok]
        out["classification"] = classification_metrics(y, p)

    if not rets.empty:
        r = rets.to_numpy(dtype=float)
        out["trading"] = trading_metrics(
            returns=r,
            weights_wide=None,
            one_way_cost_bp=0.0,
        )

        # Bootstrap CI on Sharpe
        pt, lo, hi = bootstrap_sharpe_ci(
            r,
            n_resamples=n_resamples,
            ci=ci,
            seed=seed,
            block=5,
        )
        out["sharpe_ci"] = {
            "point": float(pt),
            "lower": float(lo),
            "upper": float(hi),
            "ci": float(ci),
            "block": 5,
            "n_resamples": int(n_resamples),
        }

    return out


# ---------------------------------------------------------------------
# Yearly Sharpe
# ---------------------------------------------------------------------


def _yearly_sharpe(fold_results: list[FoldResult]) -> dict[str, float]:
    rets = _concat_net_returns(fold_results)
    if rets.empty:
        return {}
    df = rets.to_frame("net")
    df["year"] = pd.to_datetime(df.index).year
    out: dict[str, float] = {}
    for year, g in df.groupby("year"):
        out[str(int(year))] = float(sharpe(g["net"].to_numpy()))
    return out


# ---------------------------------------------------------------------
# Deflated Sharpe
# ---------------------------------------------------------------------


def _deflated_sharpe_for_model(
    fold_results: list[FoldResult],
    n_trials: int,
) -> dict[str, float]:
    rets = _concat_net_returns(fold_results)
    if rets.empty:
        return {
            "sharpe": float("nan"),
            "deflated_sharpe": float("nan"),
            "n_trials": n_trials,
            "n_obs": 0,
        }
    r = rets.to_numpy(dtype=float)
    # daily Sharpe for DSR
    mu = r.mean()
    sd = r.std(ddof=1) if r.size > 1 else 0.0
    sr_daily = mu / sd if sd > 0 else 0.0
    skew, kurt = realized_skew_kurt(r)
    dsr = deflated_sharpe(
        observed_sharpe=sr_daily,
        n_trials=n_trials,
        n_obs=int(r.size),
        skew=skew,
        kurt=kurt,
    )
    return {
        "sharpe_daily": float(sr_daily),
        "sharpe_annualized": float(sr_daily * np.sqrt(252)),
        "deflated_sharpe": float(dsr),
        "n_trials": n_trials,
        "n_obs": int(r.size),
        "skew": float(skew),
        "kurt": float(kurt),
    }


# ---------------------------------------------------------------------
# Public summary
# ---------------------------------------------------------------------


def summarize(
    result: RunResult,
    n_trials_main: int | None = None,
    bootstrap_resamples: int = 1000,
    bootstrap_ci: float = 0.95,
    seed: int = 42,
) -> dict[str, Any]:
    """Assemble the summary object for one run.

    Parameters
    ----------
    n_trials_main
        Effective number of specifications evaluated. If None,
        defaults to 4 (B0, B1, B3, main; B2 excluded as benchmark).
    """
    if n_trials_main is None:
        n_trials_main = 4

    summary: dict[str, Any] = {
        "run_id": result.run_id,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "elapsed_sec": result.elapsed_sec,
        "n_folds": len(result.folds),
        "models": {},
    }

    # Main
    summary["models"]["main"] = {
        "aggregate": _aggregate_model(result.main),
        "pooled": _pooled_metrics(
            result.main,
            bootstrap_resamples,
            bootstrap_ci,
            seed,
        ),
        "yearly_sharpe": _yearly_sharpe(result.main),
        "deflated": _deflated_sharpe_for_model(result.main, n_trials_main),
    }

    # Baselines
    for name, br in result.baselines.items():
        summary["models"][name] = {
            "aggregate": _aggregate_model(br.fold_results),
            "pooled": _pooled_metrics(
                br.fold_results,
                bootstrap_resamples,
                bootstrap_ci,
                seed,
            ),
            "yearly_sharpe": _yearly_sharpe(br.fold_results),
            # No deflated Sharpe for baselines (they are not tuned);
            # reported as reference.
            "deflated": _deflated_sharpe_for_model(
                br.fold_results,
                n_trials=n_trials_main,
            ),
        }

    return summary


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------


def _write_summary_json(summary: dict[str, Any], path: Path) -> None:
    path.write_text(
        json.dumps(summary, indent=2, default=str),
        encoding="utf-8",
    )


def _per_model_table(summary: dict[str, Any], key: str) -> pd.DataFrame:
    """Build a DataFrame with one row per model, columns for median/IQR."""
    rows = []
    for model, m in summary["models"].items():
        row = {"model": model}
        if key == "pooled":
            pooled = m["pooled"]
            cls = pooled.get("classification", {})
            trd = pooled.get("trading", {})
            row["n_obs"] = pooled.get("n_obs", 0)
            row["n_days"] = pooled.get("n_days", 0)
            for k in ("log_loss", "brier", "auc", "hit_rate"):
                row[f"cls_{k}"] = cls.get(k, float("nan"))
            for k in ("sharpe", "cagr", "max_drawdown", "total_return"):
                row[f"trd_{k}"] = trd.get(k, float("nan"))
            si = pooled.get("sharpe_ci", {})
            row["sharpe_lo"] = si.get("lower", float("nan"))
            row["sharpe_hi"] = si.get("upper", float("nan"))
        elif key == "deflated":
            d = m["deflated"]
            row["sharpe_daily"] = d.get("sharpe_daily", float("nan"))
            row["sharpe_annualized"] = d.get("sharpe_annualized", float("nan"))
            row["deflated_sharpe"] = d.get("deflated_sharpe", float("nan"))
            row["n_trials"] = d.get("n_trials", 0)
            row["n_obs"] = d.get("n_obs", 0)
        elif key == "aggregate":
            agg = m["aggregate"]
            row["n_folds"] = agg.get("n_folds", 0)
            for k in ("auc", "log_loss", "brier", "hit_rate"):
                mi = agg.get(f"cls_{k}_median_iqr", {})
                row[f"cls_{k}_median"] = mi.get("median", float("nan"))
                row[f"cls_{k}_iqr"] = mi.get("iqr", float("nan"))
            for k in ("sharpe", "cagr", "max_drawdown"):
                mi = agg.get(f"trd_{k}_median_iqr", {})
                row[f"trd_{k}_median"] = mi.get("median", float("nan"))
                row[f"trd_{k}_iqr"] = mi.get("iqr", float("nan"))
        rows.append(row)
    return pd.DataFrame(rows)


def build_report(
    result: RunResult,
    output_dir: str | Path,
    *,
    n_trials_main: int | None = None,
    bootstrap_resamples: int = 1000,
    bootstrap_ci: float = 0.95,
    seed: int = 42,
    make_plots: bool = True,
) -> Path:
    """Assemble the report and persist it next to the run artifacts.

    Returns the run directory (``output_dir/{run_id}/``).
    """
    base = Path(output_dir)
    run_dir = base / result.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    summary = summarize(
        result,
        n_trials_main=n_trials_main,
        bootstrap_resamples=bootstrap_resamples,
        bootstrap_ci=bootstrap_ci,
        seed=seed,
    )
    _write_summary_json(summary, run_dir / "summary.json")

    # Per-model tables
    _per_model_table(summary, "aggregate").to_parquet(run_dir / "table_aggregate.parquet")
    _per_model_table(summary, "pooled").to_parquet(run_dir / "table_pooled.parquet")
    _per_model_table(summary, "deflated").to_parquet(run_dir / "table_deflated.parquet")

    # Per-fold table (already written by runner but keep an aggregated copy)
    aggregate_fold_metrics(result).to_parquet(run_dir / "table_per_fold.parquet")

    # Plots (optional)
    if make_plots:
        try:
            _try_plots(result, run_dir)
        except Exception as e:
            logger.warning("plots failed: %s", e)

    return run_dir


# ---------------------------------------------------------------------
# Plots (matplotlib optional)
# ---------------------------------------------------------------------


def _try_plots(result: RunResult, run_dir: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # 1. Equity curves
    fig, ax = plt.subplots(figsize=(10, 5))
    for name, br in [("main", None), *list(result.baselines.items())]:
        frs = result.main if name == "main" else br.fold_results
        rets = _concat_net_returns(frs)
        if rets.empty:
            continue
        eq = (1.0 + rets).cumprod()
        ax.plot(eq.index, eq.values, label=name, linewidth=1.5)
    ax.set_title("Cumulative equity (net of cost)")
    ax.set_ylabel("Equity (start=1)")
    ax.legend(loc="best")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(run_dir / "equity_curves.png", dpi=110)
    plt.close(fig)

    # 2. Calibration curve (pooled, main)
    preds = _concat_predictions(result.main)
    if not preds.empty:
        ok = np.isfinite(preds["y_true"]) & np.isfinite(preds["prob"])
        y = preds.loc[ok, "y_true"].astype(int).to_numpy()
        p = preds.loc[ok, "prob"].to_numpy()
        # 10 bins
        bins = np.linspace(0.0, 1.0, 11)
        idx = np.clip(np.digitize(p, bins) - 1, 0, 9)
        mean_p = np.full(10, np.nan)
        mean_y = np.full(10, np.nan)
        for b in range(10):
            m = idx == b
            if m.any():
                mean_p[b] = p[m].mean()
                mean_y[b] = y[m].mean()
        fig, ax = plt.subplots(figsize=(6, 6))
        ax.plot([0, 1], [0, 1], "k--", alpha=0.5, label="perfect")
        ax.plot(mean_p, mean_y, "o-", label="main")
        ax.set_xlabel("mean predicted P(y=1)")
        ax.set_ylabel("observed frequency")
        ax.set_title("Calibration (pooled, 10 bins)")
        ax.legend()
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(run_dir / "calibration.png", dpi=110)
        plt.close(fig)


__all__ = [
    "aggregate_fold_metrics",
    "build_report",
    "summarize",
]
