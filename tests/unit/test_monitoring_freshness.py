"""Unit tests for monitoring.freshness. ADR 0019 section 3."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from monitoring import freshness
from monitoring.config import MartThreshold, MonitoringSettings

REF = date(2026, 10, 5)
TH = MartThreshold(warn_days=3, fail_days=7)


def _df_with_latest(latest: date, rows: int = 1) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime([latest - timedelta(days=i) for i in range(rows)]),
            "value": range(rows),
        }
    )


class TestCheckMart:
    def test_fresh_pass(self) -> None:
        df = _df_with_latest(REF - timedelta(days=1))
        r = freshness.check_mart(df, "fct_prices_daily", TH, reference_date=REF)
        assert r["status"] == "PASS"
        assert r["age_days"] == 1
        assert r["rows"] == 1
        assert r["latest_date"] == (REF - timedelta(days=1)).isoformat()
        assert r["mart"] == "fct_prices_daily"

    def test_boundary_pass_at_warn(self) -> None:
        df = _df_with_latest(REF - timedelta(days=3))
        r = freshness.check_mart(df, "x", TH, reference_date=REF)
        assert r["status"] == "PASS"

    def test_warn_just_after_warn(self) -> None:
        df = _df_with_latest(REF - timedelta(days=4))
        r = freshness.check_mart(df, "x", TH, reference_date=REF)
        assert r["status"] == "WARN"

    def test_warn_at_fail(self) -> None:
        df = _df_with_latest(REF - timedelta(days=7))
        r = freshness.check_mart(df, "x", TH, reference_date=REF)
        assert r["status"] == "WARN"

    def test_fail_after_fail(self) -> None:
        df = _df_with_latest(REF - timedelta(days=8))
        r = freshness.check_mart(df, "x", TH, reference_date=REF)
        assert r["status"] == "FAIL"

    def test_empty_df_fails(self) -> None:
        df = pd.DataFrame({"trade_date": pd.Series([], dtype="datetime64[ns]")})
        r = freshness.check_mart(df, "x", TH, reference_date=REF)
        assert r["status"] == "FAIL"
        assert r["rows"] == 0
        assert r["latest_date"] is None
        assert "empty" in r["reason"].lower()

    def test_missing_column_fails(self) -> None:
        df = pd.DataFrame({"foo": [1, 2, 3]})
        r = freshness.check_mart(df, "x", TH, reference_date=REF)
        assert r["status"] == "FAIL"

    def test_all_nan_dates_fails(self) -> None:
        df = pd.DataFrame({"trade_date": [pd.NaT, pd.NaT]})
        r = freshness.check_mart(df, "x", TH, reference_date=REF)
        assert r["status"] == "FAIL"
        assert "no parseable" in r["reason"].lower()

    def test_purity(self) -> None:
        df = _df_with_latest(REF - timedelta(days=1), rows=10)
        before = df.copy()
        _ = freshness.check_mart(df, "x", TH, reference_date=REF)
        pd.testing.assert_frame_equal(df, before)


class TestCheckAll:
    def test_all_fresh_overall_pass(self) -> None:
        settings = MonitoringSettings(reference_date=REF)
        marts = {
            "fct_prices_daily": _df_with_latest(REF),
            "fct_returns_daily": _df_with_latest(REF),
            "fct_macro_daily": _df_with_latest(REF - timedelta(days=20)),
            "fct_fundamentals_daily": _df_with_latest(REF - timedelta(days=60)),
        }
        out = freshness.check_all(marts, settings)
        assert out["overall"] == "PASS"
        assert len(out["marts"]) == 4
        assert out["reference_date"] == REF.isoformat()

    def test_missing_mart_fails(self) -> None:
        settings = MonitoringSettings(reference_date=REF)
        marts = {
            "fct_prices_daily": _df_with_latest(REF),
            # others missing
        }
        out = freshness.check_all(marts, settings)
        assert out["overall"] == "FAIL"
        by_name = {m["mart"]: m for m in out["marts"]}
        assert by_name["fct_prices_daily"]["status"] == "PASS"
        assert by_name["fct_macro_daily"]["status"] == "FAIL"
        assert by_name["fct_macro_daily"]["reason"] == "missing from input"

    def test_worst_status_wins(self) -> None:
        settings = MonitoringSettings(reference_date=REF)
        marts = {
            "fct_prices_daily": _df_with_latest(REF - timedelta(days=5)),  # WARN
            "fct_returns_daily": _df_with_latest(REF - timedelta(days=20)),  # FAIL
            "fct_macro_daily": _df_with_latest(REF - timedelta(days=20)),  # PASS
            "fct_fundamentals_daily": _df_with_latest(REF - timedelta(days=60)),  # PASS
        }
        out = freshness.check_all(marts, settings)
        assert out["overall"] == "FAIL"


class TestOverallStatus:
    def test_empty(self) -> None:
        assert freshness.overall_status([]) == "PASS"

    def test_all_pass(self) -> None:
        assert freshness.overall_status([{"status": "PASS"}, {"status": "PASS"}]) == "PASS"

    def test_worst_wins(self) -> None:
        assert (
            freshness.overall_status(
                [
                    {"status": "PASS"},
                    {"status": "WARN"},
                    {"status": "FAIL"},
                ]
            )
            == "FAIL"
        )

    def test_warn_beats_pass(self) -> None:
        assert (
            freshness.overall_status(
                [
                    {"status": "PASS"},
                    {"status": "WARN"},
                ]
            )
            == "WARN"
        )

    def test_status_rank_exposed(self) -> None:
        assert freshness.STATUS_RANK == {"PASS": 0, "WARN": 1, "FAIL": 2}
