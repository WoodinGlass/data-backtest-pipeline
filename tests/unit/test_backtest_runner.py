"""Integration tests for backtest.runner. ADR 0014 §1-§8."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.config import BacktestSettings
from backtest.report import (
    aggregate_fold_metrics,
    build_report,
    summarize,
)
from backtest.runner import run_backtest, save_run
from backtest.split import Fold
from risk.config import RiskSettings

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------


@pytest.fixture
def synthetic_world():
    """300 bdays, 5 stocks + SPY, learnable signal on feat1."""
    rng = np.random.default_rng(0)
    dates = pd.date_range("2023-01-02", periods=300, freq="B").date
    tickers = ["A", "B", "C", "D", "E", "SPY"]

    frows = []
    for d in dates:
        for t in tickers:
            f1 = rng.normal()
            f2 = rng.normal()
            logit = 1.5 * f1 + 0.1 * f2
            p = 1.0 / (1.0 + np.exp(-logit))
            y = int(rng.uniform() < p)
            frows.append(
                {
                    "ticker": t,
                    "trade_date": d,
                    "px_return_20d": rng.normal(0, 0.05),
                    "cs_percentile_rank_universe_20d": rng.uniform(0, 1),
                    "feat1": f1,
                    "feat2": f2,
                    "next_return": rng.normal(0.0002, 0.015),
                    "next_return_positive": y,
                }
            )
    features = pd.DataFrame(frows)

    prows = []
    for t in tickers:
        px = 100.0
        for d in dates:
            px *= 1.0 + rng.normal(0.0002, 0.015)
            prows.append({"ticker": t, "trade_date": d, "close": float(px)})
    prices = pd.DataFrame(prows)

    return features, prices


def _small_fold_gen(trade_dates):
    """3 folds: train ~150d, test ~30d, gap 6d."""
    ds = sorted(trade_dates)
    folds = []
    for k in range(3):
        ts = 160 + k * 40
        te = ts + 29
        trs = ts - 156
        tre = trs + 149
        if te >= len(ds):
            break
        folds.append(
            Fold(
                fold_id=k,
                train_start=ds[trs],
                train_end=ds[tre],
                test_start=ds[ts],
                test_end=ds[te],
                n_train_days=150,
                n_test_days=30,
            )
        )
    return folds


@pytest.fixture
def small_run(synthetic_world):
    features, prices = synthetic_world
    bs = BacktestSettings(use_time_decay=False)
    rs = RiskSettings()
    return run_backtest(
        features,
        prices,
        bs,
        rs,
        fold_generator=_small_fold_gen,
    )


# ---------------------------------------------------------------------
# run_backtest
# ---------------------------------------------------------------------


class TestRunBacktest:
    def test_returns_all_models(self, small_run) -> None:
        assert len(small_run.main) == 3
        assert set(small_run.baselines.keys()) == {
            "b0_naive",
            "b1_momentum",
            "b2_spy",
            "b3_top10",
        }

    def test_main_fold_columns(self, small_run) -> None:
        fr = small_run.main[0]
        for c in ("ticker", "trade_date", "prob", "next_return", "y_true"):
            assert c in fr.predictions.columns

    def test_prediction_count(self, small_run) -> None:
        # 30 test days x 6 tickers
        for fr in small_run.main:
            assert len(fr.predictions) == 30 * 6

    def test_fold_dates_consistent(self, small_run) -> None:
        for fr in small_run.main:
            assert fr.train_start <= fr.train_end < fr.test_start <= fr.test_end

    def test_deterministic(self, synthetic_world) -> None:
        features, prices = synthetic_world
        bs = BacktestSettings(use_time_decay=False)
        rs = RiskSettings()
        a = run_backtest(features, prices, bs, rs, fold_generator=_small_fold_gen)
        b = run_backtest(features, prices, bs, rs, fold_generator=_small_fold_gen)
        for fa, fb in zip(a.main, b.main, strict=True):
            np.testing.assert_allclose(
                fa.predictions["prob"].to_numpy(),
                fb.predictions["prob"].to_numpy(),
                atol=1e-12,
            )

    def test_prefix_stability(self, synthetic_world) -> None:
        """Fold 0 predictions must not depend on future data."""
        features, prices = synthetic_world
        bs = BacktestSettings(use_time_decay=False)
        rs = RiskSettings()

        full = run_backtest(
            features,
            prices,
            bs,
            rs,
            fold_generator=_small_fold_gen,
        )
        f0_end = full.folds[0].test_end

        trunc_feat = features[features["trade_date"] <= f0_end].copy()
        trunc_prices = prices[prices["trade_date"] <= f0_end].copy()

        def only_first_fold(_):
            return [full.folds[0]]

        trunc = run_backtest(
            trunc_feat,
            trunc_prices,
            bs,
            rs,
            fold_generator=only_first_fold,
        )

        p_full = (
            full.main[0].predictions.sort_values(["ticker", "trade_date"]).reset_index(drop=True)
        )
        p_trunc = (
            trunc.main[0].predictions.sort_values(["ticker", "trade_date"]).reset_index(drop=True)
        )
        np.testing.assert_allclose(
            p_full["prob"].to_numpy(),
            p_trunc["prob"].to_numpy(),
            atol=1e-12,
        )

    def test_empty_features_raises(self, synthetic_world) -> None:
        _, prices = synthetic_world
        with pytest.raises(ValueError, match="features is empty"):
            run_backtest(pd.DataFrame(), prices)

    def test_missing_columns_raises(self, synthetic_world) -> None:
        features, prices = synthetic_world
        with pytest.raises(ValueError, match="missing required"):
            run_backtest(
                features.drop(columns=["next_return_positive"]),
                prices,
            )

    def test_risk_version_mismatch(self, synthetic_world) -> None:
        features, prices = synthetic_world
        bs = BacktestSettings(risk_framework_version="v2")
        rs = RiskSettings()
        with pytest.raises(ValueError, match="version mismatch"):
            run_backtest(features, prices, bs, rs, fold_generator=_small_fold_gen)


# ---------------------------------------------------------------------
# save_run
# ---------------------------------------------------------------------


class TestSaveRun:
    def test_creates_all_artifacts(self, small_run, tmp_path: Path) -> None:
        run_dir = save_run(small_run, tmp_path)
        assert run_dir.exists()
        for f in [
            "config.json",
            "split.json",
            "metrics.parquet",
            "predictions",
            "returns",
            "baseline_predictions",
        ]:
            assert (run_dir / f).exists(), f"missing {f}"

    def test_config_json_parses(self, small_run, tmp_path: Path) -> None:
        run_dir = save_run(small_run, tmp_path)
        cfg = json.loads((run_dir / "config.json").read_text())
        assert cfg["run_id"] == small_run.run_id
        assert "backtest" in cfg
        assert "risk" in cfg

    def test_predictions_one_file_per_fold(
        self,
        small_run,
        tmp_path: Path,
    ) -> None:
        run_dir = save_run(small_run, tmp_path)
        preds = sorted((run_dir / "predictions").glob("fold_*.parquet"))
        assert len(preds) == len(small_run.main)


# ---------------------------------------------------------------------
# summarize / build_report
# ---------------------------------------------------------------------


class TestSummary:
    def test_top_keys(self, small_run) -> None:
        s = summarize(small_run, bootstrap_resamples=100)
        assert {"run_id", "n_folds", "models"} <= set(s.keys())

    def test_main_has_all_sections(self, small_run) -> None:
        s = summarize(small_run, bootstrap_resamples=100)
        main = s["models"]["main"]
        for k in ("aggregate", "pooled", "yearly_sharpe", "deflated"):
            assert k in main

    def test_baselines_present(self, small_run) -> None:
        s = summarize(small_run, bootstrap_resamples=100)
        for name in ("b0_naive", "b1_momentum", "b2_spy", "b3_top10"):
            assert name in s["models"]

    def test_pooled_n_obs(self, small_run) -> None:
        s = summarize(small_run, bootstrap_resamples=100)
        # 3 folds x 30 days x 6 tickers
        assert s["models"]["main"]["pooled"]["n_obs"] == 3 * 30 * 6


class TestBuildReport:
    def test_writes_all_files(self, small_run, tmp_path: Path) -> None:
        run_dir = build_report(
            small_run,
            tmp_path,
            bootstrap_resamples=100,
            make_plots=False,
        )
        for f in [
            "summary.json",
            "table_aggregate.parquet",
            "table_pooled.parquet",
            "table_deflated.parquet",
            "table_per_fold.parquet",
        ]:
            assert (run_dir / f).exists(), f"missing {f}"

    def test_aggregate_fold_metrics(self, small_run) -> None:
        afm = aggregate_fold_metrics(small_run)
        # 5 models (main + 4 baselines) x 3 folds
        assert afm.shape[0] == 5 * 3
        assert set(afm["model"].unique()) == {
            "main",
            "b0_naive",
            "b1_momentum",
            "b2_spy",
            "b3_top10",
        }
