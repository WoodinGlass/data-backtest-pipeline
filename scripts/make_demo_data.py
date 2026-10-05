#!/usr/bin/env python
"""Generate the synthetic sample data used by the Streamlit dashboard.

ADR 0020 sections 3-4. Deterministic (seeded). Re-run any time to
regenerate data_demo/ from scratch.

Usage:
    python scripts/make_demo_data.py
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]

TICKERS = ["AAPL", "MSFT", "SPY"]
BENCHMARK = "SPY"
SECTORS = {"AAPL": "Technology", "MSFT": "Technology", "SPY": "Benchmark"}
N_DAYS = 200
N_FOLDS = 8


def make_prices(seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-01", periods=N_DAYS).date
    rows = []
    for ticker in TICKERS:
        drift = 0.0004 if ticker == "SPY" else 0.0007
        vol = 0.008 if ticker == "SPY" else 0.015
        px = 100.0
        for d in dates:
            px *= 1.0 + rng.normal(drift, vol)
            rows.append(
                {
                    "ticker": ticker,
                    "trade_date": d,
                    "close": round(float(px), 4),
                    "sector": SECTORS[ticker],
                    "is_benchmark": ticker == BENCHMARK,
                }
            )
    return pd.DataFrame(rows)


def make_returns(prices: pd.DataFrame) -> pd.DataFrame:
    df = prices.sort_values(["ticker", "trade_date"]).copy()
    df["log_return"] = df.groupby("ticker")["close"].transform(lambda s: np.log(s / s.shift(1)))
    df["next_return"] = df.groupby("ticker")["log_return"].shift(-1)
    # Nullable boolean: True/False where next_return exists, NA otherwise.
    df["next_return_positive"] = pd.array(
        np.where(df["next_return"].isna(), pd.NA, df["next_return"] > 0),
        dtype="boolean",
    )
    return df[
        [
            "ticker",
            "trade_date",
            "close",
            "log_return",
            "next_return",
            "next_return_positive",
            "sector",
            "is_benchmark",
        ]
    ]


def make_macro(dates: list[date], seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed + 1)
    n = len(dates)

    def _walk(start: float, drift: float, vol: float) -> np.ndarray:
        steps = rng.normal(drift, vol, n)
        return start + np.cumsum(steps)

    return pd.DataFrame(
        {
            "trade_date": dates,
            "mc_fedfunds": np.round(_walk(5.0, 0.0, 0.01), 4),
            "mc_dgs10": np.round(_walk(4.2, 0.0, 0.03), 4),
            "mc_dgs2": np.round(_walk(4.5, 0.0, 0.03), 4),
            "mc_cpi_yoy": np.round(_walk(3.0, 0.0, 0.02), 4),
            "mc_payrolls_yoy": np.round(_walk(1.8, 0.0, 0.02), 4),
            "mc_unemployment": np.round(_walk(4.0, 0.0, 0.02), 4),
        }
    )


def make_fundamentals(dates: list[date], seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed + 2)
    rows = []
    for ticker in TICKERS:
        base = 400_000 if ticker == "AAPL" else 250_000
        for d in dates:
            rows.append(
                {
                    "ticker": ticker,
                    "trade_date": d,
                    "fd_net_margin": round(float(rng.normal(0.22, 0.01)), 4),
                    "fd_roe": round(float(rng.normal(0.35, 0.02)), 4),
                    "fd_capex_intensity": round(float(rng.normal(0.06, 0.005)), 4),
                    "fd_revenue_yoy": round(float(rng.normal(0.08, 0.02)), 4),
                    "fd_employees": float(base),
                }
            )
    return pd.DataFrame(rows)


def build_warehouse(
    out_dir: Path,
    prices: pd.DataFrame,
    returns: pd.DataFrame,
    macro: pd.DataFrame,
    fund: pd.DataFrame,
) -> Path:
    """Write marts as Parquet files under warehouse_demo/."""
    wh_dir = out_dir / "warehouse_demo"
    if wh_dir.exists():
        shutil.rmtree(wh_dir)
    wh_dir.mkdir(parents=True)
    prices.to_parquet(wh_dir / "fct_prices_daily.parquet", index=False)
    returns.to_parquet(wh_dir / "fct_returns_daily.parquet", index=False)
    macro.to_parquet(wh_dir / "fct_macro_daily.parquet", index=False)
    fund.to_parquet(wh_dir / "fct_fundamentals_daily.parquet", index=False)
    return wh_dir


def build_features(prices: pd.DataFrame, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed + 3)
    df = prices.sort_values(["ticker", "trade_date"]).copy()
    df["px_return_1d"] = df.groupby("ticker")["close"].transform(lambda s: np.log(s / s.shift(1)))
    df["px_vol_20d"] = df.groupby("ticker")["px_return_1d"].transform(
        lambda s: s.rolling(20, min_periods=5).std() * np.sqrt(252)
    )
    df["px_momentum_60d"] = df.groupby("ticker")["close"].transform(lambda s: s / s.shift(60) - 1.0)
    df["cs_percentile_rank_universe_20d"] = rng.uniform(0, 1, len(df))
    df["mkt_beta_60d"] = rng.normal(1.0, 0.15, len(df))
    df["mc_fedfunds"] = 5.0
    df["next_return"] = df.groupby("ticker")["px_return_1d"].shift(-1)
    df["next_return_positive"] = (df["next_return"] > 0).astype("Int64")
    df = df.dropna(subset=["next_return_positive"]).reset_index(drop=True)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df[
        [
            "ticker",
            "trade_date",
            "px_return_1d",
            "px_vol_20d",
            "px_momentum_60d",
            "cs_percentile_rank_universe_20d",
            "mkt_beta_60d",
            "mc_fedfunds",
            "next_return",
            "next_return_positive",
        ]
    ]


def build_backtest_run(out_dir: Path, features: pd.DataFrame, seed: int) -> Path:
    rng = np.random.default_rng(seed + 4)
    run_dir = out_dir / "backtest" / "20260101_120000_logistic_C0.1_hl252"
    run_dir.mkdir(parents=True, exist_ok=True)

    dates = sorted(features["trade_date"].unique())

    models = ["main", "b0_naive", "b1_momentum", "b2_spy", "b3_top10"]
    rows = []
    for m in models:
        for fid in range(N_FOLDS):
            is_main = m == "main"
            rows.append(
                {
                    "model": m,
                    "fold_id": fid,
                    "test_start": str(dates[min(fid * 20, len(dates) - 1)].date()),
                    "test_end": str(dates[min((fid + 1) * 20, len(dates) - 1)].date()),
                    "cls_log_loss": round(float(rng.normal(0.693, 0.006 if is_main else 0.002)), 6),
                    "cls_brier": round(float(rng.normal(0.25, 0.004 if is_main else 0.002)), 6),
                    "cls_auc": round(
                        float(
                            rng.normal(
                                0.515 if is_main else 0.501,
                                0.012 if is_main else 0.005,
                            )
                        ),
                        6,
                    ),
                    "cls_hit_rate": round(float(rng.normal(0.505, 0.01 if is_main else 0.005)), 6),
                    "trd_sharpe": round(
                        float(
                            rng.normal(
                                0.35 if is_main else 0.05,
                                0.6 if is_main else 0.3,
                            )
                        ),
                        6,
                    ),
                    "trd_cagr": round(float(rng.normal(0.04, 0.02)), 6),
                    "trd_max_drawdown": round(float(abs(rng.normal(0.10, 0.03))), 6),
                    "trd_total_return": round(float(rng.normal(0.05, 0.03)), 6),
                    "trd_avg_daily_turnover": round(float(abs(rng.normal(0.008, 0.002))), 6),
                    "trd_annual_turnover": round(float(abs(rng.normal(2.0, 0.3))), 6),
                    "trd_total_cost_bp": round(float(abs(rng.normal(30.0, 5.0))), 6),
                }
            )
    metrics_df = pd.DataFrame(rows)
    metrics_df.to_parquet(run_dir / "metrics.parquet", index=False)
    metrics_df.to_parquet(run_dir / "table_per_fold.parquet", index=False)

    # Pooled summary table (dashboard reads this).
    pooled = pd.DataFrame(
        [
            {
                "model": "main",
                "n_obs": len(features),
                "n_days": N_DAYS,
                "cls_auc": 0.515,
                "cls_log_loss": 0.6925,
                "cls_brier": 0.249,
                "cls_hit_rate": 0.506,
                "trd_sharpe": 0.38,
                "trd_cagr": 0.042,
                "trd_max_drawdown": 0.098,
                "trd_total_return": 0.047,
                "sharpe_lo": -0.35,
                "sharpe_hi": 1.12,
            },
            {
                "model": "b0_naive",
                "n_obs": len(features),
                "n_days": N_DAYS,
                "cls_auc": 0.500,
                "cls_log_loss": 0.6931,
                "cls_brier": 0.250,
                "cls_hit_rate": 0.500,
                "trd_sharpe": 0.0,
                "trd_cagr": 0.0,
                "trd_max_drawdown": 0.0,
                "trd_total_return": 0.0,
                "sharpe_lo": 0.0,
                "sharpe_hi": 0.0,
            },
            {
                "model": "b1_momentum",
                "n_obs": len(features),
                "n_days": N_DAYS,
                "cls_auc": 0.503,
                "cls_log_loss": 0.6930,
                "cls_brier": 0.250,
                "cls_hit_rate": 0.502,
                "trd_sharpe": 0.15,
                "trd_cagr": 0.015,
                "trd_max_drawdown": 0.08,
                "trd_total_return": 0.018,
                "sharpe_lo": -0.75,
                "sharpe_hi": 0.95,
            },
            {
                "model": "b2_spy",
                "n_obs": len(features),
                "n_days": N_DAYS,
                "cls_auc": 0.500,
                "cls_log_loss": 0.6931,
                "cls_brier": 0.250,
                "cls_hit_rate": 0.500,
                "trd_sharpe": 0.62,
                "trd_cagr": 0.075,
                "trd_max_drawdown": 0.11,
                "trd_total_return": 0.082,
                "sharpe_lo": -0.55,
                "sharpe_hi": 1.80,
            },
            {
                "model": "b3_top10",
                "n_obs": len(features),
                "n_days": N_DAYS,
                "cls_auc": 0.507,
                "cls_log_loss": 0.6928,
                "cls_brier": 0.250,
                "cls_hit_rate": 0.503,
                "trd_sharpe": 0.22,
                "trd_cagr": 0.022,
                "trd_max_drawdown": 0.09,
                "trd_total_return": 0.025,
                "sharpe_lo": -0.62,
                "sharpe_hi": 1.10,
            },
        ]
    )
    pooled.to_parquet(run_dir / "table_pooled.parquet", index=False)

    deflated = pd.DataFrame(
        [
            {
                "model": "main",
                "sharpe_daily": 0.024,
                "sharpe_annualized": 0.38,
                "deflated_sharpe": 0.52,
                "n_trials": 4,
                "n_obs": N_DAYS * 3,
            },
            {
                "model": "b2_spy",
                "sharpe_daily": 0.039,
                "sharpe_annualized": 0.62,
                "deflated_sharpe": 0.71,
                "n_trials": 4,
                "n_obs": N_DAYS,
            },
        ]
    )
    deflated.to_parquet(run_dir / "table_deflated.parquet", index=False)
    metrics_df.head(5).to_parquet(run_dir / "table_aggregate.parquet", index=False)

    # config.json
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "started_at": "2026-01-01T12:00:00+00:00",
                "finished_at": "2026-01-01T12:00:45+00:00",
                "elapsed_sec": 45.0,
                "backtest_version": "v1",
                "risk_version": "v1",
                "backtest": {
                    "train_window_months": 36,
                    "test_window_months": 3,
                    "step_months": 3,
                    "model_type": "logistic",
                    "model_C": 0.1,
                    "seed": 42,
                },
                "risk": {"kelly_fraction": 0.25, "kelly_cap": 0.05, "round_trip_cost_bp": 5.0},
                "_demo": True,
            },
            indent=2,
        )
    )

    # summary.json
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "run_id": run_dir.name,
                "started_at": "2026-01-01T12:00:00+00:00",
                "finished_at": "2026-01-01T12:00:45+00:00",
                "elapsed_sec": 45.0,
                "n_folds": N_FOLDS,
                "models": {
                    "main": {
                        "aggregate": {"n_folds": N_FOLDS},
                        "pooled": {
                            "n_obs": len(features),
                            "n_days": N_DAYS,
                            "classification": {
                                "log_loss": 0.6925,
                                "brier": 0.249,
                                "auc": 0.515,
                                "hit_rate": 0.506,
                            },
                            "trading": {"sharpe": 0.38, "cagr": 0.042, "max_drawdown": 0.098},
                            "sharpe_ci": {"lower": -0.35, "upper": 1.12},
                        },
                        "deflated": {
                            "sharpe_annualized": 0.38,
                            "deflated_sharpe": 0.52,
                            "n_trials": 4,
                        },
                        "yearly_sharpe": {"2024": 0.42},
                    }
                },
                "_demo": True,
            },
            indent=2,
        )
    )

    # split.json
    splits = []
    for fid in range(N_FOLDS):
        test_start_idx = min(fid * 20, len(dates) - 25)
        test_end_idx = min(test_start_idx + 20, len(dates) - 1)
        splits.append(
            {
                "fold_id": fid,
                "train_start": str(dates[0].date()),
                "train_end": str(dates[max(0, test_start_idx - 1)].date()),
                "test_start": str(dates[test_start_idx].date()),
                "test_end": str(dates[test_end_idx].date()),
                "n_train_days": test_start_idx,
                "n_test_days": test_end_idx - test_start_idx,
            }
        )
    (run_dir / "split.json").write_text(json.dumps(splits, indent=2))

    # predictions + returns
    (run_dir / "predictions").mkdir(exist_ok=True)
    (run_dir / "returns").mkdir(exist_ok=True)
    (run_dir / "baseline_predictions").mkdir(exist_ok=True)

    preds = pd.DataFrame(
        {
            "ticker": rng.choice(TICKERS, size=100),
            "trade_date": pd.to_datetime(rng.choice(dates, size=100)),
            "prob": np.round(rng.uniform(0.4, 0.65, 100), 4),
            "next_return": np.round(rng.normal(0, 0.01, 100), 6),
            "y_true": rng.integers(0, 2, 100),
            "fold_id": rng.integers(0, N_FOLDS, 100),
        }
    )
    for fid in range(N_FOLDS):
        preds[preds["fold_id"] == fid].to_parquet(
            run_dir / "predictions" / f"fold_{fid:02d}.parquet",
            index=False,
        )

    returns = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(sorted(dates)[: N_DAYS - 1]),
            "gross": np.round(rng.normal(0.0003, 0.008, N_DAYS - 1), 6),
            "cost": np.round(abs(rng.normal(0.00002, 0.00001, N_DAYS - 1)), 6),
            "net": np.round(rng.normal(0.00028, 0.008, N_DAYS - 1), 6),
            "cash_ret": np.round(rng.normal(0.00002, 0.000005, N_DAYS - 1), 6),
            "fold_id": np.tile(np.arange(N_FOLDS), N_DAYS)[: N_DAYS - 1],
        }
    )
    for fid in range(N_FOLDS):
        sub = returns[returns["fold_id"] == fid]
        if len(sub) > 0:
            sub.to_parquet(
                run_dir / "returns" / f"fold_{fid:02d}.parquet",
                index=False,
            )

    _make_plots(run_dir, returns)
    return run_dir


def _make_plots(run_dir: Path, returns: pd.DataFrame) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 4))
    eq_main = (1.0 + returns["net"]).cumprod()
    eq_spy = (1.0 + returns["gross"]).cumprod()
    ax.plot(returns["trade_date"], eq_main, label="main", linewidth=1.5)
    ax.plot(returns["trade_date"], eq_spy, label="SPY", linewidth=1.5, alpha=0.7)
    ax.set_title("DEMO DATA - Equity curves")
    ax.set_ylabel("Equity (start=1)")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(run_dir / "equity_curves.png", dpi=100)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(5, 5))
    grid = np.linspace(0, 1, 11)
    mid = (grid[:-1] + grid[1:]) / 2
    rng2 = np.random.default_rng(42)
    observed = np.clip(mid + rng2.normal(0, 0.02, 10), 0, 1)
    ax.plot([0, 1], [0, 1], "k--", alpha=0.5, label="perfect")
    ax.plot(mid, observed, "o-", label="main")
    ax.set_xlabel("mean predicted P(y=1)")
    ax.set_ylabel("observed frequency")
    ax.set_title("DEMO DATA - Calibration")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(run_dir / "calibration.png", dpi=100)
    plt.close(fig)


def build_monitoring(out_dir: Path, features: pd.DataFrame) -> Path:
    latest = features["trade_date"].max().date()
    report = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "version": "v1",
        "reference_date": str(latest),
        "repo_root": "(demo)",
        "freshness": {
            "reference_date": str(latest),
            "marts": [
                {
                    "mart": "fct_prices_daily",
                    "status": "PASS",
                    "rows": 600,
                    "latest_date": str(latest),
                    "age_days": 1,
                    "warn_days": 3,
                    "fail_days": 7,
                    "reason": "demo",
                },
                {
                    "mart": "fct_returns_daily",
                    "status": "PASS",
                    "rows": 594,
                    "latest_date": str(latest),
                    "age_days": 1,
                    "warn_days": 3,
                    "fail_days": 7,
                    "reason": "demo",
                },
                {
                    "mart": "fct_macro_daily",
                    "status": "PASS",
                    "rows": 600,
                    "latest_date": str(latest),
                    "age_days": 1,
                    "warn_days": 45,
                    "fail_days": 90,
                    "reason": "demo",
                },
                {
                    "mart": "fct_fundamentals_daily",
                    "status": "PASS",
                    "rows": 600,
                    "latest_date": str(latest),
                    "age_days": 1,
                    "warn_days": 120,
                    "fail_days": 180,
                    "reason": "demo",
                },
            ],
            "overall": "PASS",
        },
        "drift": {
            "n_features": 6,
            "flagged_count": 1,
            "overall": "WARN",
            "features": [
                {
                    "feature": "px_return_1d",
                    "status": "PASS",
                    "psi": 0.04,
                    "ks_pvalue": 0.62,
                    "n_ref": 30,
                    "n_cur": 30,
                    "reason": "psi=0.04, ks_p=0.62",
                },
                {
                    "feature": "px_vol_20d",
                    "status": "WARN",
                    "psi": 0.14,
                    "ks_pvalue": 0.03,
                    "n_ref": 30,
                    "n_cur": 30,
                    "reason": "psi=0.14, ks_p=0.03",
                },
                {
                    "feature": "px_momentum_60d",
                    "status": "PASS",
                    "psi": 0.03,
                    "ks_pvalue": 0.71,
                    "n_ref": 30,
                    "n_cur": 30,
                    "reason": "psi=0.03, ks_p=0.71",
                },
                {
                    "feature": "cs_percentile_rank_universe_20d",
                    "status": "PASS",
                    "psi": 0.02,
                    "ks_pvalue": 0.88,
                    "n_ref": 30,
                    "n_cur": 30,
                    "reason": "psi=0.02, ks_p=0.88",
                },
                {
                    "feature": "mkt_beta_60d",
                    "status": "PASS",
                    "psi": 0.05,
                    "ks_pvalue": 0.44,
                    "n_ref": 30,
                    "n_cur": 30,
                    "reason": "psi=0.05, ks_p=0.44",
                },
                {
                    "feature": "mc_fedfunds",
                    "status": "PASS",
                    "psi": 0.00,
                    "ks_pvalue": 1.00,
                    "n_ref": 30,
                    "n_cur": 30,
                    "reason": "psi=0.00, ks_p=1.00",
                },
            ],
        },
        "performance": {
            "run_dir": "data_demo/backtest/20260101_120000_logistic_C0.1_hl252",
            "metrics_path": "metrics.parquet",
            "model": "main",
            "window_folds": 4,
            "n_folds_total": 8,
            "n_folds_used": 4,
            "fold_range": [4, 7],
            "metrics": {
                "cls_log_loss": {
                    "value": 0.6925,
                    "warn": 0.72,
                    "fail": 0.75,
                    "direction": "lower_is_better",
                    "severity": "PASS",
                },
                "cls_brier": {
                    "value": 0.249,
                    "warn": 0.26,
                    "fail": 0.28,
                    "direction": "lower_is_better",
                    "severity": "PASS",
                },
                "cls_auc": {
                    "value": 0.515,
                    "warn": 0.50,
                    "fail": 0.48,
                    "direction": "higher_is_better",
                    "severity": "PASS",
                },
                "trd_sharpe": {
                    "value": 0.38,
                    "warn": 0.00,
                    "fail": -0.50,
                    "direction": "higher_is_better",
                    "severity": "PASS",
                },
            },
            "overall": "PASS",
            "reason": "cls_auc=0.515, cls_log_loss=0.6925, trd_sharpe=0.38",
        },
        "errors": [],
        "overall": "WARN",
        "_demo": True,
    }
    out = out_dir / "monitoring_demo.json"
    out.write_text(json.dumps(report, indent=2))
    return out


DEMO_README = """# data_demo/ - synthetic sample data

This directory contains **synthetic sample data** used by the
Streamlit dashboard when the real `data/` directory is not present
(e.g. on Streamlit Community Cloud, see ADR 0020).

**It is not real market data.** All prices, features, and metrics
are generated by `scripts/make_demo_data.py` with a fixed seed.

Regenerate with:

    python scripts/make_demo_data.py

The script is deterministic.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="data_demo")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    out = (REPO / args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    print(f"[1/6] generating synthetic data (seed={args.seed})")
    prices = make_prices(args.seed)
    returns = make_returns(prices)
    dates = sorted(prices["trade_date"].unique())
    macro = make_macro(dates, args.seed)
    fund = make_fundamentals(dates, args.seed)
    print(
        f"      prices={len(prices)}  returns={len(returns)}  macro={len(macro)}  fund={len(fund)}"
    )

    print("[2/6] building warehouse")
    wh = build_warehouse(out, prices, returns, macro, fund)
    print(f"      {wh.relative_to(REPO)}  ({wh.stat().st_size:,} B)")

    print("[3/6] building features")
    features = build_features(prices, args.seed)
    fp = out / "features_demo.parquet"
    features.to_parquet(fp, index=False)
    print(f"      {fp.relative_to(REPO)}  ({fp.stat().st_size:,} B)")

    print("[4/6] building backtest run")
    run_dir = build_backtest_run(out, features, args.seed)
    print(f"      {run_dir.relative_to(REPO)}")

    print("[5/6] building monitoring report")
    mp = build_monitoring(out, features)
    print(f"      {mp.relative_to(REPO)}  ({mp.stat().st_size:,} B)")

    print("[6/6] writing README")
    (out / "README.md").write_text(DEMO_README)

    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    n_files = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"\n[OK] {n_files} files, {total:,} B ({total / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
