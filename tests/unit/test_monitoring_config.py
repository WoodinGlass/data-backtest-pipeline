"""Unit tests for monitoring.config. ADR 0019."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from monitoring.config import (
    MONITORING_VERSION,
    MartThreshold,
    MonitoringSettings,
)


class TestDefaults:
    def test_version(self) -> None:
        assert MonitoringSettings().version == MONITORING_VERSION == "v1"

    def test_reference_date(self) -> None:
        assert MonitoringSettings().reference_date is None

    def test_marts_set(self) -> None:
        s = MonitoringSettings()
        assert set(s.marts) == {
            "fct_prices_daily",
            "fct_returns_daily",
            "fct_macro_daily",
            "fct_fundamentals_daily",
        }

    def test_mart_thresholds(self) -> None:
        s = MonitoringSettings()
        assert s.marts["fct_prices_daily"].warn_days == 3
        assert s.marts["fct_prices_daily"].fail_days == 7
        assert s.marts["fct_macro_daily"].warn_days == 45
        assert s.marts["fct_fundamentals_daily"].fail_days == 180

    def test_drift_defaults(self) -> None:
        s = MonitoringSettings()
        assert s.drift_reference_days == 60
        assert s.drift_current_days == 60
        assert s.drift_psi_warn == 0.10
        assert s.drift_psi_fail == 0.25
        assert s.drift_ks_pvalue_warn == 0.05
        assert s.drift_ks_pvalue_fail == 0.01

    def test_drift_excluded_columns(self) -> None:
        assert MonitoringSettings().drift_exclude_columns == [
            "ticker",
            "trade_date",
            "next_return",
            "next_return_positive",
        ]

    def test_perf_window(self) -> None:
        assert MonitoringSettings().perf_window_folds == 4

    def test_paths(self) -> None:
        s = MonitoringSettings()
        assert s.warehouse_path == "data/warehouse.duckdb"
        assert s.warehouse_schema == "marts"
        assert s.features_path == "data/features/v1/features_daily.parquet"
        assert s.backtest_dir == "data/backtest"
        assert s.output_path == "reports/monitoring.json"


class TestMartThreshold:
    def test_accepts_warn_below_fail(self) -> None:
        mt = MartThreshold(warn_days=1, fail_days=3)
        assert mt.warn_days == 1
        assert mt.fail_days == 3

    def test_rejects_warn_equal_fail(self) -> None:
        with pytest.raises(ValidationError) as exc:
            MartThreshold(warn_days=5, fail_days=5)
        assert "warn_days" in str(exc.value)

    def test_rejects_warn_above_fail(self) -> None:
        with pytest.raises(ValidationError):
            MartThreshold(warn_days=10, fail_days=5)

    def test_rejects_non_positive(self) -> None:
        with pytest.raises(ValidationError):
            MartThreshold(warn_days=0, fail_days=5)
        with pytest.raises(ValidationError):
            MartThreshold(warn_days=1, fail_days=0)


class TestCrossField:
    def test_rejects_psi_warn_equal_fail(self) -> None:
        with pytest.raises(ValidationError) as exc:
            MonitoringSettings(drift_psi_warn=0.25, drift_psi_fail=0.25)
        assert "drift_psi_warn" in str(exc.value)

    def test_rejects_psi_warn_above_fail(self) -> None:
        with pytest.raises(ValidationError):
            MonitoringSettings(drift_psi_warn=0.30, drift_psi_fail=0.25)

    def test_rejects_ks_warn_below_fail(self) -> None:
        with pytest.raises(ValidationError) as exc:
            MonitoringSettings(
                drift_ks_pvalue_warn=0.001,
                drift_ks_pvalue_fail=0.01,
            )
        assert "drift_ks_pvalue_warn" in str(exc.value)

    def test_rejects_perf_window_zero(self) -> None:
        with pytest.raises(ValidationError):
            MonitoringSettings(perf_window_folds=0)


class TestDateParsing:
    def test_iso_string(self) -> None:
        s = MonitoringSettings(reference_date="2026-10-05")
        assert s.reference_date == date(2026, 10, 5)


class TestEnvOverride:
    def test_perf_window(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_MON_PERF_WINDOW_FOLDS", "8")
        assert MonitoringSettings().perf_window_folds == 8

    def test_output_path(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_MON_OUTPUT_PATH", "/tmp/x.json")
        assert MonitoringSettings().output_path == "/tmp/x.json"

    def test_kwarg_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("DBP_MON_PERF_WINDOW_FOLDS", "99")
        assert MonitoringSettings(perf_window_folds=3).perf_window_folds == 3


class TestDescribe:
    def test_contains_key_fields(self) -> None:
        s = MonitoringSettings().describe()
        assert "version=v1" in s
        assert "marts=4" in s
        assert "drift_window=60/60d" in s
        assert "psi_warn/fail=0.1/0.25" in s
        assert "perf_window=4 folds" in s
