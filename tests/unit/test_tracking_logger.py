"""Unit tests for tracking.logger. ADR 0015 §3-§6."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tracking.config import TrackingSettings
from tracking.logger import collect_metrics, collect_params, log_backtest_run

# --- duck-typed fixtures ---


@dataclass
class _Fold:
    fold_id: int
    trading: dict
    classification: dict


@dataclass
class _Result:
    run_id: str
    config: dict
    risk_config: dict
    main: list = field(default_factory=list)


def _result() -> _Result:
    return _Result(
        run_id="20261004_120000_logistic_C0.1_hl252",
        config={
            "train_window_months": 36,
            "test_window_months": 3,
            "model_type": "logistic",
            "model_C": 0.1,
            "baselines": ["b0_naive", "b1_momentum"],
            "seed": 42,
        },
        risk_config={
            "kelly_fraction": 0.25,
            "kelly_cap": 0.05,
            "round_trip_cost_bp": 5.0,
        },
    )


def _summary_minimal() -> dict:
    return {
        "models": {
            "main": {
                "pooled": {
                    "classification": {
                        "log_loss": 0.6931,
                        "brier": 0.25,
                        "auc": 0.5105,
                        "hit_rate": 0.5002,
                    },
                    "trading": {
                        "sharpe": 0.42,
                        "cagr": 0.05,
                        "max_drawdown": 0.12,
                        "total_return": 0.35,
                        "avg_daily_turnover": 0.008,
                        "annual_turnover": 2.0,
                        "total_cost_bp": 15.0,
                    },
                    "sharpe_ci": {"lower": -0.2, "upper": 1.0},
                    "n_obs": 3102,
                    "n_days": 1200,
                },
                "deflated": {
                    "sharpe_annualized": 0.42,
                    "deflated_sharpe": 0.30,
                    "n_trials": 4,
                },
                "aggregate": {
                    "n_folds": 35,
                    "cls_auc_median_iqr": {"median": 0.512, "iqr": 0.02},
                    "trd_sharpe_median_iqr": {"median": 0.35, "iqr": 0.8},
                },
            },
            "b2_spy": {
                "pooled": {
                    "classification": {"auc": 0.50},
                    "trading": {"sharpe": 0.95},
                },
            },
        },
    }


# --- collect_params ---


class TestCollectParams:
    def test_prefixes_bt_and_risk(self) -> None:
        p = collect_params(_result())
        assert p["bt.train_window_months"] == "36"
        assert p["bt.model_C"] == "0.1"
        assert p["risk.kelly_fraction"] == "0.25"
        assert p["risk.round_trip_cost_bp"] == "5.0"

    def test_list_becomes_csv(self) -> None:
        p = collect_params(_result())
        assert p["bt.baselines"] == "b0_naive,b1_momentum"

    def test_includes_git_and_env(self) -> None:
        p = collect_params(_result())
        assert "git.sha" in p
        assert "git.branch" in p
        assert "git.dirty" in p
        assert "env.python" in p
        assert "env.platform" in p

    def test_none_becomes_empty_string(self) -> None:
        r = _result()
        r.config["missing"] = None
        p = collect_params(r)
        assert p["bt.missing"] == ""


# --- collect_metrics ---


class TestCollectMetrics:
    def test_pooled_keys(self) -> None:
        m = collect_metrics(_result(), _summary_minimal(), TrackingSettings())
        assert m["pooled/sharpe"] == 0.42
        assert m["pooled/auc"] == 0.5105
        assert m["pooled/sharpe_ci_lower"] == -0.2
        assert m["pooled/sharpe_ci_upper"] == 1.0

    def test_deflated_keys(self) -> None:
        m = collect_metrics(_result(), _summary_minimal(), TrackingSettings())
        assert m["deflated/deflated_sharpe"] == 0.30
        assert m["deflated/n_trials"] == 4.0

    def test_aggregate_keys(self) -> None:
        m = collect_metrics(_result(), _summary_minimal(), TrackingSettings())
        assert m["agg/cls_auc_median"] == 0.512
        assert m["agg/cls_auc_iqr"] == 0.02
        assert m["agg/trd_sharpe_median"] == 0.35
        assert m["agg/trd_sharpe_iqr"] == 0.8

    def test_baseline_keys(self) -> None:
        m = collect_metrics(_result(), _summary_minimal(), TrackingSettings())
        assert m["baseline/b2_spy/auc"] == 0.50
        assert m["baseline/b2_spy/sharpe"] == 0.95

    def test_nan_and_none_skipped(self) -> None:
        s = _summary_minimal()
        s["models"]["main"]["pooled"]["classification"]["auc"] = float("nan")
        s["models"]["main"]["pooled"]["trading"]["sharpe"] = None
        m = collect_metrics(_result(), s, TrackingSettings())
        assert "pooled/auc" not in m
        assert "pooled/sharpe" not in m

    def test_empty_summary_returns_no_metrics(self) -> None:
        m = collect_metrics(_result(), {"models": {}}, TrackingSettings())
        assert m == {}

    def test_fold_metrics_when_enabled(self) -> None:
        r = _result()
        r.main = [
            _Fold(0, {"sharpe": 0.11}, {"auc": 0.505}),
            _Fold(1, {"sharpe": 0.22}, {"auc": 0.515}),
        ]
        ts = TrackingSettings(log_fold_metrics=True, max_fold_metrics=100)
        m = collect_metrics(r, _summary_minimal(), ts)
        assert m["fold/00/sharpe"] == 0.11
        assert m["fold/00/auc"] == 0.505
        assert m["fold/01/sharpe"] == 0.22
        assert m["fold/01/auc"] == 0.515

    def test_fold_metrics_disabled(self) -> None:
        r = _result()
        r.main = [_Fold(0, {"sharpe": 0.11}, {"auc": 0.505})]
        ts = TrackingSettings(log_fold_metrics=False)
        m = collect_metrics(r, _summary_minimal(), ts)
        assert not any(k.startswith("fold/") for k in m)

    def test_fold_metrics_capped(self) -> None:
        r = _result()
        r.main = [_Fold(i, {"sharpe": 0.1}, {"auc": 0.5}) for i in range(50)]
        ts = TrackingSettings(log_fold_metrics=True, max_fold_metrics=4)
        m = collect_metrics(r, _summary_minimal(), ts)
        fold_keys = [k for k in m if k.startswith("fold/")]
        assert len(fold_keys) <= 4


# --- log_backtest_run disabled path ---


class TestLogBacktestRun:
    def test_disabled_returns_none(self) -> None:
        rid = log_backtest_run(
            _result(),
            _summary_minimal(),
            run_dir=Path("/tmp/does_not_exist"),
            settings=TrackingSettings(enabled=False),
        )
        assert rid is None

    def test_enabled_without_mlflow_returns_none(self, monkeypatch) -> None:
        from tracking import logger

        monkeypatch.setattr(logger, "MLFLOW_AVAILABLE", False)
        rid = log_backtest_run(
            _result(),
            _summary_minimal(),
            run_dir=Path("/tmp/does_not_exist"),
            settings=TrackingSettings(enabled=True),
        )
        assert rid is None
