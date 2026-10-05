"""Unit tests for monitoring.drift. ADR 0019 section 4-5."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from monitoring import drift
from monitoring.config import MonitoringSettings


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(0)


class TestComputePSI:
    def test_identical_distributions_near_zero(self, rng) -> None:
        ref = rng.normal(0, 1, 5000)
        cur = rng.normal(0, 1, 5000)
        psi = drift.compute_psi(ref, cur)
        assert np.isfinite(psi)
        assert psi < 0.05

    def test_large_shift_high_psi(self, rng) -> None:
        ref = rng.normal(0, 1, 5000)
        cur = rng.normal(2, 1, 5000)
        psi = drift.compute_psi(ref, cur)
        assert psi > 0.25

    def test_small_shift_between(self, rng) -> None:
        ref = rng.normal(0, 1, 5000)
        cur = rng.normal(0.3, 1, 5000)
        psi_small = drift.compute_psi(ref, cur)
        cur_big = rng.normal(2, 1, 5000)
        psi_big = drift.compute_psi(ref, cur_big)
        assert 0 <= psi_small <= psi_big

    def test_constant_arrays_nan(self) -> None:
        assert np.isnan(
            drift.compute_psi(
                np.array([1.0, 1.0, 1.0]),
                np.array([1.0, 1.0]),
            )
        )

    def test_too_few_values_nan(self) -> None:
        assert np.isnan(drift.compute_psi(np.array([1.0]), np.array([1.0, 2.0])))

    def test_nan_filtered(self) -> None:
        ref = np.array([1.0, np.nan, 2.0, 3.0, np.nan, 4.0])
        cur = np.array([1.0, 2.0, 3.0])
        assert np.isfinite(drift.compute_psi(ref, cur))


class TestComputeKS:
    def test_identical_low_stat_high_p(self, rng) -> None:
        ref = rng.normal(0, 1, 5000)
        cur = rng.normal(0, 1, 5000)
        stat, p = drift.compute_ks(ref, cur)
        assert stat < 0.05
        assert p > 0.05

    def test_shift_high_stat_low_p(self, rng) -> None:
        ref = rng.normal(0, 1, 5000)
        cur = rng.normal(2, 1, 5000)
        stat, p = drift.compute_ks(ref, cur)
        assert stat > 0.5
        assert p < 1e-6

    def test_degenerate_nan(self) -> None:
        stat, p = drift.compute_ks(np.array([1.0]), np.array([1.0, 2.0]))
        assert np.isnan(stat)
        assert np.isnan(p)


class TestCheckFeature:
    def test_identical_pass(self, rng) -> None:
        s_ref = pd.Series(rng.normal(0, 1, 2000), name="feat")
        s_cur = pd.Series(rng.normal(0, 1, 2000), name="feat")
        r = drift.check_feature(s_ref, s_cur, MonitoringSettings())
        assert r["feature"] == "feat"
        assert r["status"] == "PASS"
        assert r["psi_severity"] == "PASS"
        assert r["ks_severity"] == "PASS"
        assert r["n_ref"] == 2000
        assert r["n_cur"] == 2000

    def test_large_shift_fail(self, rng) -> None:
        s_ref = pd.Series(rng.normal(0, 1, 2000), name="feat")
        s_cur = pd.Series(rng.normal(2, 1, 2000), name="feat")
        r = drift.check_feature(s_ref, s_cur, MonitoringSettings())
        assert r["status"] == "FAIL"
        assert r["psi_severity"] == "FAIL"
        assert r["ks_severity"] == "FAIL"

    def test_small_shift_warn(self, rng) -> None:
        # Shift enough to trigger PSI warn but not fail
        s_ref = pd.Series(rng.normal(0, 1, 5000), name="feat")
        s_cur = pd.Series(rng.normal(0.5, 1, 5000), name="feat")
        r = drift.check_feature(s_ref, s_cur, MonitoringSettings())
        # psi ~0.3 typically; but this test only asserts status is WARN or FAIL
        assert r["status"] in ("WARN", "FAIL")

    def test_non_numeric_pass(self) -> None:
        s = pd.Series(["a", "b", "c"], name="label")
        r = drift.check_feature(s, s, MonitoringSettings())
        assert r["status"] == "PASS"
        assert r["reason"] == "non-numeric"

    def test_insufficient_data_pass(self) -> None:
        s = pd.Series([1.0], name="short")
        r = drift.check_feature(s, s, MonitoringSettings())
        assert r["status"] == "PASS"
        assert r["reason"] == "insufficient data"

    def test_nan_only_insufficient(self) -> None:
        s_ref = pd.Series([np.nan, np.nan], name="f")
        s_cur = pd.Series([1.0, 2.0, 3.0], name="f")
        r = drift.check_feature(s_ref, s_cur, MonitoringSettings())
        assert r["status"] == "PASS"
        assert r["reason"] == "insufficient data"

    def test_purity(self, rng) -> None:
        s_ref = pd.Series(rng.normal(0, 1, 100), name="f")
        s_cur = pd.Series(rng.normal(0, 1, 100), name="f")
        before_ref = s_ref.copy()
        before_cur = s_cur.copy()
        _ = drift.check_feature(s_ref, s_cur, MonitoringSettings())
        pd.testing.assert_series_equal(s_ref, before_ref)
        pd.testing.assert_series_equal(s_cur, before_cur)


class TestCheckAll:
    def _frames(self, rng) -> tuple[pd.DataFrame, pd.DataFrame]:
        n = 500
        ref = pd.DataFrame(
            {
                "ticker": "AAPL",
                "trade_date": pd.date_range("2024-01-01", periods=n, freq="D"),
                "feat_a": rng.normal(0, 1, n),
                "feat_b": rng.normal(0, 1, n),
                "feat_c": rng.normal(0, 1, n),
                "next_return": rng.normal(0, 0.01, n),
                "next_return_positive": rng.integers(0, 2, n),
            }
        )
        cur = pd.DataFrame(
            {
                "ticker": "AAPL",
                "trade_date": pd.date_range("2025-01-01", periods=n, freq="D"),
                "feat_a": rng.normal(0, 1, n),
                "feat_b": rng.normal(2, 1, n),  # shifted
                "feat_c": rng.normal(0, 1, n),
            }
        )
        return ref, cur

    def test_excludes_configured_columns(self, rng) -> None:
        ref, cur = self._frames(rng)
        out = drift.check_all(ref, cur, MonitoringSettings())
        names = {d["feature"] for d in out["features"]}
        assert "feat_a" in names
        assert "feat_b" in names
        assert "feat_c" in names
        assert "ticker" not in names
        assert "trade_date" not in names
        assert "next_return" not in names
        assert "next_return_positive" not in names

    def test_detects_shifted_feature(self, rng) -> None:
        ref, cur = self._frames(rng)
        out = drift.check_all(ref, cur, MonitoringSettings())
        by = {d["feature"]: d for d in out["features"]}
        assert by["feat_a"]["status"] == "PASS"
        assert by["feat_b"]["status"] == "FAIL"
        assert by["feat_c"]["status"] == "PASS"
        assert out["overall"] == "FAIL"
        assert out["flagged_count"] >= 1

    def test_missing_feature_in_one_window(self, rng) -> None:
        ref, cur = self._frames(rng)
        cur = cur.drop(columns=["feat_c"])
        out = drift.check_all(ref, cur, MonitoringSettings())
        by = {d["feature"]: d for d in out["features"]}
        assert by["feat_c"]["status"] == "PASS"
        assert by["feat_c"]["reason"] == "missing in one window"


class TestOverallStatus:
    def test_empty(self) -> None:
        assert drift.overall_status([]) == "PASS"

    def test_worst_wins(self) -> None:
        assert (
            drift.overall_status(
                [
                    {"status": "PASS"},
                    {"status": "WARN"},
                    {"status": "FAIL"},
                ]
            )
            == "FAIL"
        )

    def test_status_rank_exposed(self) -> None:
        assert drift.STATUS_RANK == {"PASS": 0, "WARN": 1, "FAIL": 2}
