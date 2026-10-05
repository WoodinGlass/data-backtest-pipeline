"""Unit tests for monitoring.performance. ADR 0019 section 6."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from monitoring import performance
from monitoring.config import MonitoringSettings


def _metrics_df(
    model: str = "main",
    n_folds: int = 8,
    *,
    auc: list[float] | None = None,
    log_loss: list[float] | None = None,
    brier: list[float] | None = None,
    sharpe: list[float] | None = None,
) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    return pd.DataFrame(
        {
            "model": [model] * n_folds,
            "fold_id": list(range(n_folds)),
            "cls_auc": auc or list(rng.uniform(0.50, 0.55, n_folds)),
            "cls_log_loss": log_loss or list(rng.uniform(0.68, 0.71, n_folds)),
            "cls_brier": brier or list(rng.uniform(0.23, 0.25, n_folds)),
            "trd_sharpe": sharpe or list(rng.uniform(-0.5, 1.0, n_folds)),
        }
    )


class TestSeverityBoundaries:
    def test_lower_is_better(self) -> None:
        # log_loss: warn 0.72, fail 0.75
        assert performance._severity(0.70, 0.72, 0.75, "lower_is_better") == "PASS"
        assert performance._severity(0.72, 0.72, 0.75, "lower_is_better") == "WARN"
        assert performance._severity(0.74, 0.72, 0.75, "lower_is_better") == "WARN"
        assert performance._severity(0.75, 0.72, 0.75, "lower_is_better") == "FAIL"
        assert performance._severity(0.80, 0.72, 0.75, "lower_is_better") == "FAIL"

    def test_higher_is_better(self) -> None:
        # auc: warn 0.50, fail 0.48
        assert performance._severity(0.55, 0.50, 0.48, "higher_is_better") == "PASS"
        assert performance._severity(0.51, 0.50, 0.48, "higher_is_better") == "PASS"
        assert performance._severity(0.50, 0.50, 0.48, "higher_is_better") == "WARN"
        assert performance._severity(0.49, 0.50, 0.48, "higher_is_better") == "WARN"
        assert performance._severity(0.48, 0.50, 0.48, "higher_is_better") == "FAIL"
        assert performance._severity(0.40, 0.50, 0.48, "higher_is_better") == "FAIL"

    def test_nan_pass(self) -> None:
        assert performance._severity(float("nan"), 0.72, 0.75, "lower_is_better") == "PASS"


class TestRollingMean:
    def test_uses_last_n(self) -> None:
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        assert performance._rolling_mean(s, 2) == pytest.approx(4.5)
        assert performance._rolling_mean(s, 5) == pytest.approx(3.0)
        assert performance._rolling_mean(s, 10) == pytest.approx(3.0)

    def test_empty_nan(self) -> None:
        assert np.isnan(performance._rolling_mean(pd.Series([], dtype=float), 3))

    def test_nan_dropped(self) -> None:
        s = pd.Series([1.0, np.nan, 3.0, np.nan, 5.0])
        # dropna -> [1, 3, 5], last 2 -> mean = 4.0
        assert performance._rolling_mean(s, 2) == pytest.approx(4.0)


class TestCheckFoldMetrics:
    def test_happy_path(self) -> None:
        df = _metrics_df(n_folds=8)
        settings = MonitoringSettings(perf_window_folds=4)
        r = performance.check_fold_metrics(df, settings)
        assert r["model"] == "main"
        assert r["n_folds_total"] == 8
        assert r["n_folds_used"] == 4
        assert r["fold_range"] == [4, 7]
        assert set(r["metrics"]) == {
            "cls_auc",
            "cls_log_loss",
            "cls_brier",
            "trd_sharpe",
        }

    def test_bad_metrics_fail(self) -> None:
        df = _metrics_df(
            n_folds=8,
            auc=[0.55, 0.53, 0.52, 0.51, 0.49, 0.47, 0.45, 0.44],
            log_loss=[0.69, 0.70, 0.71, 0.72, 0.74, 0.76, 0.78, 0.80],
            brier=[0.24, 0.25, 0.26, 0.27, 0.28, 0.29, 0.30, 0.31],
            sharpe=[0.5, 0.3, 0.1, -0.1, -0.3, -0.5, -0.7, -0.9],
        )
        settings = MonitoringSettings(perf_window_folds=4)
        r = performance.check_fold_metrics(df, settings)
        assert r["overall"] == "FAIL"
        assert r["metrics"]["cls_auc"]["severity"] == "FAIL"
        assert r["metrics"]["cls_log_loss"]["severity"] == "FAIL"
        assert r["metrics"]["cls_brier"]["severity"] == "FAIL"
        assert r["metrics"]["trd_sharpe"]["severity"] == "FAIL"

    def test_warn_zone(self) -> None:
        df = _metrics_df(
            n_folds=4,
            auc=[0.51, 0.51, 0.50, 0.50],  # mean 0.505 -> PASS
            log_loss=[0.71, 0.72, 0.72, 0.73],  # mean 0.72 -> WARN
            brier=[0.24, 0.24, 0.25, 0.25],  # mean 0.245 -> PASS
            sharpe=[0.5, 0.3, 0.2, 0.1],  # mean 0.275 -> PASS
        )
        settings = MonitoringSettings(perf_window_folds=4)
        r = performance.check_fold_metrics(df, settings)
        assert r["metrics"]["cls_log_loss"]["severity"] == "WARN"
        assert r["metrics"]["cls_auc"]["severity"] == "PASS"
        assert r["overall"] == "WARN"

    def test_empty_df(self) -> None:
        r = performance.check_fold_metrics(pd.DataFrame(), MonitoringSettings())
        assert r["overall"] == "PASS"
        assert "empty" in r["reason"]

    def test_missing_model(self) -> None:
        df = _metrics_df(model="b2_spy", n_folds=4)
        r = performance.check_fold_metrics(df, MonitoringSettings(), model="main")
        assert "no rows for model" in r["reason"]
        assert r["overall"] == "PASS"

    def test_missing_metric_columns(self) -> None:
        df = pd.DataFrame(
            {
                "model": ["main"] * 4,
                "fold_id": [0, 1, 2, 3],
            }
        )
        r = performance.check_fold_metrics(df, MonitoringSettings())
        assert r["metrics"] == {}
        assert r["overall"] == "PASS"
        assert "no tracked metric columns" in r["reason"]

    def test_window_larger_than_data(self) -> None:
        df = _metrics_df(n_folds=3)
        settings = MonitoringSettings(perf_window_folds=10)
        r = performance.check_fold_metrics(df, settings)
        assert r["n_folds_used"] == 3
        assert r["fold_range"] == [0, 2]

    def test_purity(self) -> None:
        df = _metrics_df(n_folds=8)
        before = df.copy()
        _ = performance.check_fold_metrics(df, MonitoringSettings())
        pd.testing.assert_frame_equal(df, before)


class TestCheckRunDir:
    def test_missing_metrics_file_fails(self, tmp_path) -> None:
        r = performance.check_run_dir(tmp_path, MonitoringSettings())
        assert r["overall"] == "FAIL"
        assert r["reason"] == "metrics.parquet not found"
        assert r["run_dir"] == str(tmp_path)

    def test_loads_and_checks(self, tmp_path) -> None:
        df = _metrics_df(n_folds=8)
        df.to_parquet(tmp_path / "metrics.parquet")
        r = performance.check_run_dir(tmp_path, MonitoringSettings())
        assert r["overall"] in ("PASS", "WARN", "FAIL")
        assert r["metrics_path"].endswith("metrics.parquet")
        assert r["n_folds_total"] == 8


class TestOverallStatus:
    def test_empty(self) -> None:
        assert performance.overall_status({}) == "PASS"

    def test_falls_back_to_top_level(self) -> None:
        assert performance.overall_status({"overall": "WARN"}) == "WARN"

    def test_worst_of_metrics(self) -> None:
        assert (
            performance.overall_status(
                {
                    "metrics": {
                        "a": {"severity": "PASS"},
                        "b": {"severity": "FAIL"},
                        "c": {"severity": "WARN"},
                    },
                }
            )
            == "FAIL"
        )
