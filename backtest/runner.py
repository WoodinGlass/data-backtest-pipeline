"""Walk-forward runner for M5. Contract: ADR 0014 §7, §8.

Public API:

    run_backtest(features, prices, settings, risk_settings,
                 cash_rates=None, fold_generator=None) -> RunResult

    save_run(result, output_dir) -> Path

Pipeline per fold:
    1. Slice train/test from `features` using fold dates.
    2. Fit logistic regression on train.
    3. Predict P(y=1) on test rows.
    4. Signal baselines (B1, B3) compute their own test probs.
    5. Portfolio baselines (B0, B2) build test weights directly.
    6. Run the pipeline: entry -> staking -> limits -> portfolio.
    7. Compute classification + trading metrics.

Everything is deterministic given (features, prices, settings, seed).
No IO except `save_run`.

Time zones and label alignment:
    - Features are PIT (M4, ADR 0012).
    - Labels at t describe the return from t to t+1.
    - Portfolio is committed at end of day t and earns next_return[t].
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest.baselines import (
    available_baselines,
    build_baseline,
    is_portfolio_baseline,
)
from backtest.config import BACKTEST_VERSION, BacktestSettings
from backtest.metrics import (
    classification_metrics,
    trading_metrics,
)
from backtest.model import fit_predict
from backtest.portfolio import (
    PortfolioResult,
    run_portfolio,
    run_portfolio_from_weights,
)
from backtest.split import Fold, generate_folds
from risk.config import RISK_FRAMEWORK_VERSION, RiskSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------


@dataclass
class FoldResult:
    """Results for one fold, one model (main or baseline)."""

    fold_id: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    n_train_days: int
    n_test_days: int
    predictions: pd.DataFrame  # ticker, trade_date, prob, next_return, y_true
    returns: pd.DataFrame  # index=trade_date; cols gross, cost, net, cash_ret
    weights: pd.DataFrame  # wide, index=trade_date, cols=ticker
    classification: dict[str, float]
    trading: dict[str, float]
    n_positions: pd.Series
    cash_weight: pd.Series


@dataclass
class BaselineResult:
    name: str
    is_portfolio: bool
    fold_results: list[FoldResult]


@dataclass
class RunResult:
    run_id: str
    started_at: str
    finished_at: str
    elapsed_sec: float
    config: dict[str, Any]
    risk_config: dict[str, Any]
    folds: list[Fold]
    main: list[FoldResult]
    baselines: dict[str, BaselineResult] = field(default_factory=dict)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _make_run_id(settings: BacktestSettings, _risk: RiskSettings) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    tag = f"{settings.model_type}_C{settings.model_C}_hl{settings.time_decay_half_life_days}"
    return f"{ts}_{tag}"


def _config_dict(settings: BacktestSettings) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in settings.model_dump().items():
        if isinstance(v, date):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _risk_config_dict(risk: RiskSettings) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in risk.model_dump().items():
        out[k] = v
    out["round_trip_cost_bp"] = risk.round_trip_cost_bp
    return out


def _slice_features(
    features: pd.DataFrame,
    start: date,
    end: date,
) -> pd.DataFrame:
    return features[(features["trade_date"] >= start) & (features["trade_date"] <= end)].copy()


def _slice_prices(
    prices: pd.DataFrame,
    start: date,
    end: date,
) -> pd.DataFrame:
    return prices[(prices["trade_date"] >= start) & (prices["trade_date"] <= end)].copy()


# ---------------------------------------------------------------------
# Per-fold runners
# ---------------------------------------------------------------------


def _run_signal_fold(
    _fold: Fold,
    test_predictions: pd.DataFrame,
    prices_test: pd.DataFrame,
    next_returns_test: pd.DataFrame,
    risk_settings: RiskSettings,
    cash_rates: pd.Series | None,
) -> tuple[PortfolioResult, pd.DataFrame]:
    """Run the risk pipeline (entry -> staking -> limits -> portfolio).

    Returns (portfolio_result, predictions_with_side).
    """
    if test_predictions.empty or next_returns_test.empty:
        empty_pr = PortfolioResult(
            returns=pd.DataFrame(columns=["gross", "cost", "net", "cash_ret"]),
            weights=pd.DataFrame(),
            turnover=pd.Series(dtype=float),
            n_positions=pd.Series(dtype=int),
            n_days=0,
            cash_weight=pd.Series(dtype=float),
        )
        return empty_pr, test_predictions

    pr = run_portfolio(
        predictions=test_predictions,
        prices=prices_test,
        next_returns=next_returns_test,
        risk_settings=risk_settings,
        cash_annual_rates=cash_rates,
    )
    return pr, test_predictions


def _run_portfolio_fold(
    target_weights_wide: pd.DataFrame,
    next_returns_wide: pd.DataFrame,
    risk_settings: RiskSettings,
    cash_rates: pd.Series | None,
) -> PortfolioResult:
    if target_weights_wide.empty or next_returns_wide.empty:
        return PortfolioResult(
            returns=pd.DataFrame(columns=["gross", "cost", "net", "cash_ret"]),
            weights=pd.DataFrame(),
            turnover=pd.Series(dtype=float),
            n_positions=pd.Series(dtype=int),
            n_days=0,
            cash_weight=pd.Series(dtype=float),
        )
    return run_portfolio_from_weights(
        target_weights=target_weights_wide,
        next_returns=next_returns_wide,
        risk_settings=risk_settings,
        cash_annual_rates=cash_rates,
    )


# ---------------------------------------------------------------------
# Metric assembly
# ---------------------------------------------------------------------


def _classification_from_predictions(
    preds_with_truth: pd.DataFrame,
) -> dict[str, float]:
    y = preds_with_truth["y_true"].to_numpy()
    p = preds_with_truth["prob"].to_numpy()
    return classification_metrics(y, p)


def _trading_from_portfolio(
    pr: PortfolioResult,
    risk: RiskSettings,
) -> dict[str, float]:
    if pr.returns.empty:
        return {
            "sharpe": float("nan"),
            "cagr": float("nan"),
            "max_drawdown": float("nan"),
            "total_return": float("nan"),
            "n_days": 0,
            "avg_daily_turnover": float("nan"),
            "annual_turnover": float("nan"),
            "total_cost_bp": float("nan"),
        }
    return trading_metrics(
        returns=pr.returns["net"].to_numpy(),
        weights_wide=pr.weights,
        one_way_cost_bp=risk.one_way_cost_bp,
    )


# ---------------------------------------------------------------------
# Baseline preparation (per fold)
# ---------------------------------------------------------------------


def _prepare_baseline_fold(
    name: str,
    fold: Fold,
    test_features: pd.DataFrame,
    test_prices: pd.DataFrame,
    test_next_returns_long: pd.DataFrame,
    settings: BacktestSettings,
    risk: RiskSettings,
    cash_rates: pd.Series | None,
) -> FoldResult:
    """Run one baseline for one fold."""
    baseline_df = build_baseline(name, test_features, settings)

    if is_portfolio_baseline(name):
        # Wide weights directly
        if baseline_df.empty:
            target_wide = pd.DataFrame()
        else:
            target_wide = (
                baseline_df.pivot(
                    index="trade_date",
                    columns="ticker",
                    values="weight",
                )
                .sort_index()
                .fillna(0.0)
            )
        nr_wide = test_next_returns_long.pivot(
            index="trade_date",
            columns="ticker",
            values="next_return",
        ).sort_index()
        pr = _run_portfolio_fold(
            target_wide,
            nr_wide,
            risk,
            cash_rates,
        )
        # For portfolio baselines we still record predictions (prob only)
        preds_with_truth = _attach_truth(
            baseline_df,
            test_next_returns_long,
            settings.label_column,
            test_features,
        )
    else:
        pr, _ = _run_signal_fold(
            fold,
            baseline_df,
            test_prices,
            test_next_returns_long,
            risk,
            cash_rates,
        )
        preds_with_truth = _attach_truth(
            baseline_df,
            test_next_returns_long,
            settings.label_column,
            test_features,
        )

    cls = _classification_from_predictions(preds_with_truth)
    trd = _trading_from_portfolio(pr, risk)

    return FoldResult(
        fold_id=fold.fold_id,
        train_start=fold.train_start,
        train_end=fold.train_end,
        test_start=fold.test_start,
        test_end=fold.test_end,
        n_train_days=fold.n_train_days,
        n_test_days=fold.n_test_days,
        predictions=preds_with_truth,
        returns=pr.returns,
        weights=pr.weights,
        classification=cls,
        trading=trd,
        n_positions=pr.n_positions,
        cash_weight=pr.cash_weight,
    )


def _attach_truth(
    predictions_df: pd.DataFrame,
    next_returns_long: pd.DataFrame,
    label_col: str,
    test_features: pd.DataFrame,
) -> pd.DataFrame:
    """Attach next_return and y_true to a predictions frame."""
    keys = ["ticker", "trade_date"]
    truth = next_returns_long[[*keys, "next_return"]].copy()
    # y_true from features (label)
    labels = test_features[[*keys, label_col]].copy()
    labels = labels.rename(columns={label_col: "y_true"})

    out = predictions_df[[*keys, "prob"]].copy()
    out = out.merge(truth, on=keys, how="left")
    return out.merge(labels, on=keys, how="left")


# ---------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------


def run_backtest(
    features: pd.DataFrame,
    prices: pd.DataFrame,
    settings: BacktestSettings | None = None,
    risk_settings: RiskSettings | None = None,
    cash_rates: pd.Series | None = None,
    fold_generator=None,
) -> RunResult:
    """Run the full walk-forward backtest.

    Parameters
    ----------
    features
        Long: ticker, trade_date, <feature cols>, next_return, label.
    prices
        Long: ticker, trade_date, close. Used for risk/limits.
    settings, risk_settings
        Configuration. Defaults used if None.
    cash_rates
        Series index=trade_date, annualized rate (e.g. 0.05). Optional.
    fold_generator
        Callable(trade_dates) -> list[Fold]. If None, uses
        backtest.split.generate_folds.
    """
    t0 = time.time()
    started_at = datetime.utcnow().isoformat(timespec="seconds")

    if settings is None:
        settings = BacktestSettings()
    if risk_settings is None:
        risk_settings = RiskSettings()

    # Sanity: risk framework version
    if risk_settings.version != settings.risk_framework_version:
        raise ValueError(
            f"risk framework version mismatch: "
            f"RiskSettings.version={risk_settings.version!r}, "
            f"BacktestSettings.risk_framework_version="
            f"{settings.risk_framework_version!r}"
        )

    # Validate inputs
    if features.empty:
        raise ValueError("features is empty")
    if prices.empty:
        raise ValueError("prices is empty")
    required_feat = {"ticker", "trade_date", "next_return", settings.label_column}
    missing = required_feat - set(features.columns)
    if missing:
        raise ValueError(
            f"features missing required columns: {sorted(missing)}. "
            f"Expected columns include 'next_return' (raw log return) and "
            f"the label column."
        )

    # Set global seeds (determinism)
    import random

    random.seed(settings.seed)
    np.random.seed(settings.seed)

    # Determine feature columns (exclude keys, label, next_return)
    excl = {"ticker", "trade_date", "next_return", settings.label_column}
    feature_cols = [c for c in features.columns if c not in excl]

    logger.info(
        "run_backtest: %d rows, %d features, %d trade dates",
        len(features),
        len(feature_cols),
        features["trade_date"].nunique(),
    )

    # Folds
    trade_dates = sorted(features["trade_date"].unique().tolist())
    if fold_generator is None:
        folds = generate_folds(trade_dates, settings)
    else:
        folds = fold_generator(trade_dates)
    logger.info("run_backtest: %d folds", len(folds))

    run_id = _make_run_id(settings, risk_settings)

    main_results: list[FoldResult] = []
    baseline_results: dict[str, BaselineResult] = {
        name: BaselineResult(
            name=name,
            is_portfolio=is_portfolio_baseline(name),
            fold_results=[],
        )
        for name in settings.baselines
        if name in available_baselines()
    }

    # Per-fold iteration
    for fold in folds:
        train_feat = _slice_features(features, fold.train_start, fold.train_end)
        test_feat = _slice_features(features, fold.test_start, fold.test_end)
        test_prices = _slice_prices(prices, fold.test_start, fold.test_end)

        if train_feat.empty or test_feat.empty:
            logger.warning("fold %d: empty train or test slice", fold.fold_id)
            continue

        # --- MAIN MODEL ---
        probs = fit_predict(train_feat, test_feat, feature_cols, settings)
        main_preds = test_feat[["ticker", "trade_date"]].copy()
        main_preds["prob"] = probs
        main_preds["is_benchmark"] = (test_feat["ticker"] == settings.benchmark_ticker).values

        next_ret_long = test_feat[["ticker", "trade_date", "next_return"]].copy()

        pr_main, _ = _run_signal_fold(
            fold,
            main_preds,
            test_prices,
            next_ret_long,
            risk_settings,
            cash_rates,
        )
        main_with_truth = _attach_truth(
            main_preds,
            next_ret_long,
            settings.label_column,
            test_feat,
        )
        cls_main = _classification_from_predictions(main_with_truth)
        trd_main = _trading_from_portfolio(pr_main, risk_settings)

        main_results.append(
            FoldResult(
                fold_id=fold.fold_id,
                train_start=fold.train_start,
                train_end=fold.train_end,
                test_start=fold.test_start,
                test_end=fold.test_end,
                n_train_days=fold.n_train_days,
                n_test_days=fold.n_test_days,
                predictions=main_with_truth,
                returns=pr_main.returns,
                weights=pr_main.weights,
                classification=cls_main,
                trading=trd_main,
                n_positions=pr_main.n_positions,
                cash_weight=pr_main.cash_weight,
            )
        )

        # --- BASELINES ---
        for name in baseline_results:
            try:
                bres = _prepare_baseline_fold(
                    name=name,
                    fold=fold,
                    test_features=test_feat,
                    test_prices=test_prices,
                    test_next_returns_long=next_ret_long,
                    settings=settings,
                    risk=risk_settings,
                    cash_rates=cash_rates,
                )
                baseline_results[name].fold_results.append(bres)
            except Exception as e:
                logger.warning(
                    "baseline %s fold %d failed: %s",
                    name,
                    fold.fold_id,
                    e,
                )

        logger.info(
            "fold %d done: main auc=%.4f, sharpe=%.3f",
            fold.fold_id,
            cls_main.get("auc", float("nan")),
            trd_main.get("sharpe", float("nan")),
        )

    finished_at = datetime.utcnow().isoformat(timespec="seconds")
    elapsed = time.time() - t0
    logger.info("run_backtest done in %.1fs", elapsed)

    return RunResult(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        elapsed_sec=elapsed,
        config=_config_dict(settings),
        risk_config=_risk_config_dict(risk_settings),
        folds=folds,
        main=main_results,
        baselines=baseline_results,
    )


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------


def _fold_result_to_frames(
    fr: FoldResult,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    preds = fr.predictions.copy()
    preds["fold_id"] = fr.fold_id
    returns = fr.returns.copy().reset_index()
    returns["fold_id"] = fr.fold_id
    return preds, returns


def save_run(result: RunResult, output_dir: str | Path) -> Path:
    """Persist a RunResult to disk. Returns the run directory."""
    base = Path(output_dir)
    run_dir = base / result.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Config
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "run_id": result.run_id,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "elapsed_sec": result.elapsed_sec,
                "backtest_version": BACKTEST_VERSION,
                "risk_version": RISK_FRAMEWORK_VERSION,
                "backtest": result.config,
                "risk": result.risk_config,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    # Split
    (run_dir / "split.json").write_text(
        json.dumps(
            [
                {
                    "fold_id": f.fold_id,
                    "train_start": f.train_start.isoformat(),
                    "train_end": f.train_end.isoformat(),
                    "test_start": f.test_start.isoformat(),
                    "test_end": f.test_end.isoformat(),
                    "n_train_days": f.n_train_days,
                    "n_test_days": f.n_test_days,
                }
                for f in result.folds
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    # Predictions
    preds_dir = run_dir / "predictions"
    preds_dir.mkdir(exist_ok=True)
    for fr in result.main:
        preds, _ = _fold_result_to_frames(fr)
        preds.to_parquet(preds_dir / f"fold_{fr.fold_id:02d}.parquet")

    # Returns
    rets_dir = run_dir / "returns"
    rets_dir.mkdir(exist_ok=True)
    for fr in result.main:
        _, rets = _fold_result_to_frames(fr)
        rets.to_parquet(rets_dir / f"fold_{fr.fold_id:02d}.parquet")

    # Metrics per-fold
    metrics_rows: list[dict[str, Any]] = []
    for fr in result.main:
        row: dict[str, Any] = {
            "model": "main",
            "fold_id": fr.fold_id,
            "test_start": fr.test_start.isoformat(),
            "test_end": fr.test_end.isoformat(),
        }
        row.update({f"cls_{k}": v for k, v in fr.classification.items()})
        row.update({f"trd_{k}": v for k, v in fr.trading.items()})
        metrics_rows.append(row)

    for name, br in result.baselines.items():
        for fr in br.fold_results:
            row = {
                "model": name,
                "fold_id": fr.fold_id,
                "test_start": fr.test_start.isoformat(),
                "test_end": fr.test_end.isoformat(),
            }
            row.update({f"cls_{k}": v for k, v in fr.classification.items()})
            row.update({f"trd_{k}": v for k, v in fr.trading.items()})
            metrics_rows.append(row)

    metrics_df = pd.DataFrame(metrics_rows)
    metrics_df.to_parquet(run_dir / "metrics.parquet")

    # Baseline predictions (compact: only prob + truth, no weights)
    bl_dir = run_dir / "baseline_predictions"
    bl_dir.mkdir(exist_ok=True)
    for name, br in result.baselines.items():
        if not br.fold_results:
            continue
        parts = []
        for fr in br.fold_results:
            p = fr.predictions.copy()
            p["fold_id"] = fr.fold_id
            parts.append(p)
        pd.concat(parts, ignore_index=True).to_parquet(bl_dir / f"{name}.parquet")

    return run_dir


__all__ = [
    "BaselineResult",
    "FoldResult",
    "RunResult",
    "run_backtest",
    "save_run",
]
