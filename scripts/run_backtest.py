#!/usr/bin/env python
"""Run the M5 walk-forward backtest end-to-end.

Reads features from the M4 parquet, prices and macro from the
warehouse, then runs the walk-forward protocol from ADR 0014 and
writes artifacts to ``data/backtest/{run_id}/``.

Usage
-----
    python scripts/run_backtest.py
    python scripts/run_backtest.py --tickers AAPL MSFT --start 2018-01-01
    python scripts/run_backtest.py --no-plots --no-decay
    make backtest

Data dependencies
-----------------
- features: data/features/v1/features_daily.parquet   (M4)
- prices:   marts.fct_prices_daily in DUCKDB_PATH      (M1-M2)
- cash:     marts.fct_macro_daily.mc_fedfunds          (M3.5-M3.6)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd

# Make the repo importable when run as a script.
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run the M5 walk-forward backtest.",
    )
    p.add_argument(
        "--features",
        default="data/features/v1/features_daily.parquet",
        help="Path to features parquet (relative to repo root).",
    )
    p.add_argument(
        "--warehouse",
        default=os.environ.get(
            "DUCKDB_PATH",
            "data/warehouse.duckdb",
        ),
        help="Path to DuckDB warehouse.",
    )
    p.add_argument(
        "--out-dir",
        default="data/backtest",
        help="Base directory for run artifacts.",
    )
    p.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Optional subset of tickers (default: all in features).",
    )
    p.add_argument(
        "--start",
        default=None,
        help="Optional first trade_date (YYYY-MM-DD).",
    )
    p.add_argument(
        "--end",
        default=None,
        help="Optional last trade_date (YYYY-MM-DD).",
    )
    p.add_argument(
        "--no-decay",
        action="store_true",
        help="Disable time-decay sample weighting.",
    )
    p.add_argument(
        "--no-plots",
        action="store_true",
        help="Skip matplotlib plot generation.",
    )
    p.add_argument(
        "--bootstrap",
        type=int,
        default=1000,
        help="Bootstrap resamples for Sharpe/AUC CI.",
    )
    p.add_argument(
        "--n-trials",
        type=int,
        default=4,
        help="Effective number of specifications for deflated Sharpe.",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Global random seed.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    # --- MLflow tracking (M6) ---
    p.add_argument(
        "--mlflow",
        dest="mlflow",
        action="store_true",
        help="Enable MLflow tracking (default: off).",
    )
    p.add_argument(
        "--no-mlflow",
        dest="mlflow",
        action="store_false",
        help="Explicitly disable MLflow tracking.",
    )
    p.set_defaults(mlflow=False)
    p.add_argument(
        "--tracking-uri",
        default=None,
        help="Override MLflow tracking URI.",
    )
    p.add_argument(
        "--no-register-model",
        dest="register_model",
        action="store_false",
        help="Skip refit-on-full-data + registry step.",
    )
    p.set_defaults(register_model=True)
    return p.parse_args(argv)


def _load_features(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Features parquet not found: {path}. Run M4 first: make features")
    df = pd.read_parquet(path)
    # Coerce trade_date to date for consistency with backtest code.
    if "trade_date" in df.columns and not isinstance(
        df["trade_date"].iloc[0],
        type(pd.to_datetime("2020-01-01").date()),
    ):
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def _load_prices(warehouse: Path, tickers: list[str] | None) -> pd.DataFrame:
    import duckdb

    if not warehouse.exists():
        raise FileNotFoundError(f"Warehouse not found: {warehouse}. Run M1-M2 first.")
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        q = """
            SELECT ticker, trade_date, close
            FROM marts.fct_prices_daily
        """
        params: list = []
        if tickers:
            placeholders = ",".join(["?"] * len(tickers))
            q += f" WHERE ticker IN ({placeholders})"
            params.extend(tickers)
        q += " ORDER BY ticker, trade_date"
        df = con.execute(q, params).df()
    finally:
        con.close()
    if df.empty:
        raise ValueError("No prices found in warehouse.")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    return df


def _load_cash_rates(warehouse: Path) -> pd.Series | None:
    import duckdb

    if not warehouse.exists():
        return None
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        # fct_macro_daily is wide with columns like mc_fedfunds
        cols = con.execute("PRAGMA table_info('marts.fct_macro_daily')").df()["name"].tolist()
        col = None
        for cand in ("mc_fedfunds", "mc_fed_funds", "fed_funds", "DFF"):
            if cand in cols:
                col = cand
                break
        if col is None:
            return None
        df = con.execute(
            f"SELECT trade_date, {col} AS rate FROM marts.fct_macro_daily ORDER BY trade_date"
        ).df()
    except Exception:
        return None
    finally:
        con.close()
    if df.empty or df["rate"].isna().all():
        return None
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    # Fed funds is stored in percent (e.g. 5.25), convert to decimal.
    rate = df["rate"].astype(float)
    if rate.abs().max() > 1.0:
        rate = rate / 100.0
    return pd.Series(
        rate.fillna(0.0).to_numpy(),
        index=df["trade_date"],
        name="cash_rate",
    )


def _apply_filters(
    features: pd.DataFrame,
    tickers: list[str] | None,
    start: str | None,
    end: str | None,
) -> pd.DataFrame:
    df = features
    if tickers:
        df = df[df["ticker"].isin(tickers)]
    if start:
        s = pd.to_datetime(start).date()
        df = df[df["trade_date"] >= s]
    if end:
        e = pd.to_datetime(end).date()
        df = df[df["trade_date"] <= e]
    return df.reset_index(drop=True)


def _print_summary_table(run_dir: Path) -> None:
    """Print the pooled summary table to stdout."""
    try:
        tbl = pd.read_parquet(run_dir / "table_pooled.parquet")
    except Exception as e:
        print(f"(could not read pooled table: {e})")
        return
    display_cols = [
        c
        for c in (
            "model",
            "n_obs",
            "n_days",
            "cls_auc",
            "cls_log_loss",
            "cls_brier",
            "trd_sharpe",
            "trd_cagr",
            "trd_max_drawdown",
            "sharpe_lo",
            "sharpe_hi",
        )
        if c in tbl.columns
    ]
    show = tbl[display_cols].copy()
    for c in show.columns:
        if show[c].dtype.kind == "f":
            show[c] = show[c].round(4)
    print()
    print("=" * 100)
    print("Pooled summary (all folds concatenated)")
    print("=" * 100)
    print(show.to_string(index=False))

    # Deflated Sharpe table
    try:
        dt = pd.read_parquet(run_dir / "table_deflated.parquet")
        dcols = [
            c
            for c in (
                "model",
                "sharpe_daily",
                "sharpe_annualized",
                "deflated_sharpe",
                "n_trials",
                "n_obs",
            )
            if c in dt.columns
        ]
        dd = dt[dcols].copy()
        for c in dd.columns:
            if dd[c].dtype.kind == "f":
                dd[c] = dd[c].round(4)
        print()
        print("Deflated Sharpe (Bailey & Lopez de Prado 2014)")
        print("-" * 100)
        print(dd.to_string(index=False))
    except Exception:
        pass


def _run_tracking(
    result,
    features,
    bt_settings,
    tracking_uri,
    register_model,
    run_dir,
    logger,
    repo_root=None,
) -> None:
    """Best-effort MLflow logging + optional model registration."""
    try:
        from backtest.report import summarize
        from tracking.client import MLFLOW_AVAILABLE
        from tracking.config import TrackingSettings
        from tracking.logger import log_backtest_run
        from tracking.registry import log_and_register_model
    except ImportError as e:
        logger.warning("tracking: import failed (%s); skipping", e)
        return

    if not MLFLOW_AVAILABLE:
        logger.warning(
            "tracking: mlflow not installed; skipping. Install with `pip install -e .[tracking]`."
        )
        return

    kwargs = {"enabled": True, "register_model": register_model}
    if tracking_uri:
        kwargs["tracking_uri"] = tracking_uri
    settings = TrackingSettings(**kwargs)
    logger.info("tracking: %s", settings.describe())

    try:
        summary = summarize(
            result,
            n_trials_main=4,
            bootstrap_resamples=1000,
            bootstrap_ci=0.95,
            seed=bt_settings.seed,
        )
    except Exception as e:
        logger.warning("tracking: summarize failed (%s); skipping", e)
        return

    try:
        run_id = log_backtest_run(
            result=result,
            summary=summary,
            run_dir=run_dir,
            settings=settings,
            repo_root=repo_root,
        )
        if run_id:
            logger.info("tracking: logged walk-forward run id=%s", run_id)
        else:
            logger.warning("tracking: log_backtest_run returned None")
    except Exception as e:
        logger.error("tracking: log_backtest_run failed: %s", e)

    if register_model:
        try:
            version = log_and_register_model(
                result=result,
                features=features,
                bt_settings=bt_settings,
                tracking_settings=settings,
            )
            if version:
                logger.info(
                    "tracking: registered %s v%s as @%s",
                    settings.model_name,
                    version,
                    settings.challenger_alias,
                )
            else:
                logger.warning("tracking: log_and_register_model returned None")
        except Exception as e:
            logger.error("tracking: log_and_register_model failed: %s", e)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("run_backtest")

    features_path = Path(args.features).resolve()
    warehouse_path = Path(args.warehouse).resolve()
    out_dir = Path(args.out_dir).resolve()

    logger.info("features : %s", features_path)
    logger.info("warehouse: %s", warehouse_path)
    logger.info("out_dir  : %s", out_dir)

    # Load
    features = _load_features(features_path)
    logger.info(
        "loaded features: %d rows, %d tickers, %d dates",
        len(features),
        features["ticker"].nunique(),
        features["trade_date"].nunique(),
    )

    features = _apply_filters(
        features,
        args.tickers,
        args.start,
        args.end,
    )
    if features.empty:
        logger.error("features empty after filters")
        return 2
    logger.info("after filters: %d rows, %d dates", len(features), features["trade_date"].nunique())

    tickers_for_prices = args.tickers or sorted(features["ticker"].unique())
    prices = _load_prices(warehouse_path, tickers_for_prices)
    logger.info("loaded prices: %d rows", len(prices))

    cash_rates = _load_cash_rates(warehouse_path)
    if cash_rates is not None:
        logger.info("loaded cash rates: %d observations", len(cash_rates))
    else:
        logger.info("cash rates: none (idle cash earns 0%%)")

    # Configure
    from backtest.config import BacktestSettings
    from risk.config import RiskSettings

    bs = BacktestSettings(
        use_time_decay=not args.no_decay,
        seed=args.seed,
        bootstrap_resamples=args.bootstrap,
    )
    rs = RiskSettings()
    logger.info("backtest: %s", bs.describe())

    # Run
    from backtest.report import build_report
    from backtest.runner import run_backtest, save_run

    t0 = time.time()
    result = run_backtest(features, prices, bs, rs, cash_rates=cash_rates)
    logger.info("run_backtest complete in %.1fs", time.time() - t0)
    logger.info("folds: %d", len(result.main))

    # Persist
    run_dir = save_run(result, out_dir)
    logger.info("saved run to %s", run_dir)

    build_report(
        result,
        out_dir,
        n_trials_main=args.n_trials,
        bootstrap_resamples=args.bootstrap,
        bootstrap_ci=0.95,
        seed=args.seed,
        make_plots=not args.no_plots,
    )
    logger.info("report built")

    # --- MLflow tracking (M6, opt-in) ---
    if args.mlflow:
        _run_tracking(
            result=result,
            features=features,
            bt_settings=bs,
            tracking_uri=args.tracking_uri,
            register_model=args.register_model,
            run_dir=run_dir,
            logger=logger,
            repo_root=REPO,
        )

    _print_summary_table(run_dir)
    print()
    print(f"Artifacts: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
