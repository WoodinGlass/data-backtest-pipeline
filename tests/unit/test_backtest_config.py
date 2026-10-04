"""Unit tests for backtest.config.BacktestSettings. ADR 0014 §1-§8."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from backtest.config import BACKTEST_VERSION, BacktestSettings


class TestDefaults:
    def test_version(self) -> None:
        assert BacktestSettings().version == BACKTEST_VERSION == "v1"

    def test_split_defaults(self) -> None:
        bs = BacktestSettings()
        assert bs.train_window_months == 36
        assert bs.test_window_months == 3
        assert bs.step_months == 3
        assert bs.first_test_start is None
        assert bs.last_test_end is None

    def test_gap_defaults(self) -> None:
        bs = BacktestSettings()
        assert bs.purge_days == 1
        assert bs.embargo_days == 5
        assert bs.total_gap_days == 6

    def test_model_defaults(self) -> None:
        bs = BacktestSettings()
        assert bs.model_type == "logistic"
        assert bs.model_C == 0.1
        assert bs.model_max_iter == 1000
        assert bs.model_solver == "lbfgs"

    def test_decay_defaults(self) -> None:
        bs = BacktestSettings()
        assert bs.use_time_decay is True
        assert bs.time_decay_half_life_days == 252

    def test_baseline_defaults(self) -> None:
        bs = BacktestSettings()
        assert bs.baselines == ["b0_naive", "b1_momentum", "b2_spy", "b3_top10"]
        assert bs.momentum_lookback_days == 20
        assert bs.top_n_baseline == 10

    def test_metrics_defaults(self) -> None:
        bs = BacktestSettings()
        assert bs.bootstrap_resamples == 1000
        assert bs.bootstrap_ci == 0.95

    def test_repro_defaults(self) -> None:
        bs = BacktestSettings()
        assert bs.seed == 42
        assert bs.output_dir == "data/backtest"
        assert bs.label_column == "next_return_positive"
        assert bs.benchmark_ticker == "SPY"


class TestValidators:
    def test_rejects_step_below_test_window(self) -> None:
        with pytest.raises(ValidationError) as exc:
            BacktestSettings(step_months=1, test_window_months=3)
        assert "step_months" in str(exc.value)

    def test_rejects_purge_zero(self) -> None:
        with pytest.raises(ValidationError) as exc:
            BacktestSettings(purge_days=0)
        assert "purge_days" in str(exc.value)

    def test_rejects_first_after_last(self) -> None:
        with pytest.raises(ValidationError) as exc:
            BacktestSettings(
                first_test_start="2026-01-01",
                last_test_end="2020-01-01",
            )
        assert "first_test_start" in str(exc.value)

    def test_accepts_step_equal_test_window(self) -> None:
        bs = BacktestSettings(step_months=3, test_window_months=3)
        assert bs.step_months == 3


class TestDateParsing:
    def test_iso_strings(self) -> None:
        bs = BacktestSettings(
            first_test_start="2018-01-02",
            last_test_end="2026-09-30",
        )
        assert bs.first_test_start == date(2018, 1, 2)
        assert bs.last_test_end == date(2026, 9, 30)


class TestEnvOverride:
    def test_seed(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_BT_SEED", "123")
        assert BacktestSettings().seed == 123

    def test_half_life(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_BT_TIME_DECAY_HALF_LIFE_DAYS", "504")
        assert BacktestSettings().time_decay_half_life_days == 504

    def test_kwarg_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_BT_SEED", "999")
        assert BacktestSettings(seed=7).seed == 7


class TestDescribe:
    def test_contains_key_fields(self) -> None:
        s = BacktestSettings().describe()
        assert "version=v1" in s
        assert "train=36m" in s
        assert "test=3m" in s
        assert "purge=1d" in s
        assert "embargo=5d" in s
        assert "seed=42" in s
