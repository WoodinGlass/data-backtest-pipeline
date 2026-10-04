"""Unit tests for risk.entry. See ADR 0013 §4, §8."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from risk.config import RiskSettings
from risk.entry import (
    _ENTRY_REGISTRY,
    apply_entry_rules,
    available_entry_methods,
    register_entry,
)


def _preds() -> pd.DataFrame:
    """3 dates x 5 stocks + 1 benchmark (SPY)."""
    rows = []
    data = {
        "2024-01-02": {"A": 0.52, "B": 0.60, "C": 0.45, "D": 0.71, "E": 0.55},
        "2024-01-03": {"A": 0.48, "B": 0.49, "C": 0.51, "D": 0.53, "E": 0.90},
        "2024-01-04": {"A": 0.40, "B": 0.80, "C": 0.65, "D": 0.35, "E": 0.70},
    }
    for d, probs in data.items():
        for ticker, p in probs.items():
            rows.append({
                "ticker": ticker,
                "trade_date": pd.to_datetime(d).date(),
                "prob": p,
                "is_benchmark": False,
            })
        rows.append({
            "ticker": "SPY",
            "trade_date": pd.to_datetime(d).date(),
            "prob": 0.50,
            "is_benchmark": True,
        })
    return pd.DataFrame(rows)


class TestRegistry:
    def test_expected_methods_registered(self) -> None:
        assert set(available_entry_methods()) == {
            "threshold", "top_n", "cross_sectional",
        }

    def test_register_new_method(self) -> None:
        @register_entry("test_custom_entry")
        def _custom(preds, settings):
            out = preds.copy()
            out["side"] = "flat"
            out["selected"] = False
            return out
        try:
            assert "test_custom_entry" in available_entry_methods()
        finally:
            _ENTRY_REGISTRY.pop("test_custom_entry", None)

    def test_register_duplicate_raises(self) -> None:
        with pytest.raises(ValueError):
            @register_entry("threshold")
            def _dup(preds, settings):
                return preds


class TestThreshold:
    def test_selects_above_threshold(self) -> None:
        preds = _preds()
        rs = RiskSettings(entry_method="threshold", entry_prob_threshold=0.55)
        out = apply_entry_rules(preds, rs)

        # 2024-01-02: B(0.60), D(0.71)
        d1 = out[(out["trade_date"] == pd.to_datetime("2024-01-02").date())
                 & out["selected"]]
        assert set(d1["ticker"]) == {"B", "D"}

        # 2024-01-03: E(0.90)
        d2 = out[(out["trade_date"] == pd.to_datetime("2024-01-03").date())
                 & out["selected"]]
        assert set(d2["ticker"]) == {"E"}

        # 2024-01-04: B(0.80), C(0.65), E(0.70)
        d3 = out[(out["trade_date"] == pd.to_datetime("2024-01-04").date())
                 & out["selected"]]
        assert set(d3["ticker"]) == {"B", "C", "E"}

    def test_strict_inequality(self) -> None:
        """prob == threshold must NOT be selected."""
        preds = pd.DataFrame({
            "ticker": ["A"],
            "trade_date": [pd.to_datetime("2024-01-02").date()],
            "prob": [0.55],
        })
        rs = RiskSettings(entry_method="threshold", entry_prob_threshold=0.55)
        out = apply_entry_rules(preds, rs)
        assert out.loc[0, "side"] == "flat"


class TestTopN:
    def test_exactly_n_per_date(self) -> None:
        preds = _preds()
        rs = RiskSettings(entry_method="top_n", entry_top_n=2)
        out = apply_entry_rules(preds, rs)
        counts = out[out["selected"]].groupby("trade_date").size()
        assert (counts == 2).all()

    def test_top_n_by_prob_descending(self) -> None:
        preds = _preds()
        rs = RiskSettings(entry_method="top_n", entry_top_n=2)
        out = apply_entry_rules(preds, rs)

        d1 = out[(out["trade_date"] == pd.to_datetime("2024-01-02").date())
                 & out["selected"]]
        assert set(d1["ticker"]) == {"D", "B"}  # 0.71, 0.60

        d3 = out[(out["trade_date"] == pd.to_datetime("2024-01-04").date())
                 & out["selected"]]
        assert set(d3["ticker"]) == {"B", "E"}  # 0.80, 0.70

    def test_top_n_excludes_benchmark(self) -> None:
        preds = _preds()
        # Give SPY the highest prob to tempt the rule
        preds.loc[preds["ticker"] == "SPY", "prob"] = 0.99
        rs = RiskSettings(entry_method="top_n", entry_top_n=2)
        out = apply_entry_rules(preds, rs)
        assert not out.loc[out["ticker"] == "SPY", "selected"].any()

    def test_top_n_fewer_than_n(self) -> None:
        preds = pd.DataFrame({
            "ticker": ["A", "B"],
            "trade_date": [pd.to_datetime("2024-01-02").date()] * 2,
            "prob": [0.6, 0.7],
        })
        rs = RiskSettings(entry_method="top_n", entry_top_n=5)
        out = apply_entry_rules(preds, rs)
        assert out["selected"].sum() == 2  # only 2 rows exist


class TestCrossSectional:
    def test_above_median_per_date(self) -> None:
        preds = _preds()
        rs = RiskSettings(entry_method="cross_sectional")
        out = apply_entry_rules(preds, rs)

        for d in preds["trade_date"].unique():
            day = preds[(preds["trade_date"] == d) & (~preds["is_benchmark"])]
            median = day["prob"].median()
            expected = set(day.loc[day["prob"] > median, "ticker"])
            actual = out[(out["trade_date"] == d) & out["selected"]]
            assert set(actual["ticker"]) == expected

    def test_benchmark_excluded_from_median(self) -> None:
        # Construct a case where including SPY would shift the median.
        # A,B,C: 0.30, 0.70, 0.80 -> median 0.70 -> above: C(0.80) only
        # SPY: 0.99 -> if included, 4 values [0.30,0.70,0.80,0.99] median=0.75
        #             -> above: C(0.80), SPY(0.99) -- different!
        preds = pd.DataFrame({
            "ticker": ["A", "B", "C", "SPY"],
            "trade_date": [pd.to_datetime("2024-01-02").date()] * 4,
            "prob": [0.30, 0.70, 0.80, 0.99],
            "is_benchmark": [False, False, False, True],
        })
        rs = RiskSettings(entry_method="cross_sectional")
        out = apply_entry_rules(preds, rs)
        assert set(out.loc[out["selected"], "ticker"]) == {"C"}

    def test_benchmark_forced_flat(self) -> None:
        preds = _preds()
        preds.loc[preds["ticker"] == "SPY", "prob"] = 0.99
        rs = RiskSettings(entry_method="cross_sectional")
        out = apply_entry_rules(preds, rs)
        spy = out[out["ticker"] == "SPY"]
        assert (spy["side"] == "flat").all()


class TestPurityAndSchema:
    def test_input_not_mutated(self) -> None:
        preds = _preds()
        before = preds.copy()
        _ = apply_entry_rules(preds, RiskSettings())
        pd.testing.assert_frame_equal(preds, before)

    def test_adds_side_and_selected(self) -> None:
        preds = _preds()
        out = apply_entry_rules(preds, RiskSettings())
        assert "side" in out.columns
        assert "selected" in out.columns
        assert out["side"].isin(["long", "flat"]).all()

    def test_index_preserved(self) -> None:
        preds = _preds()
        out = apply_entry_rules(preds, RiskSettings())
        assert out.index.equals(preds.index)

    def test_deterministic(self) -> None:
        preds = _preds()
        rs = RiskSettings()
        a = apply_entry_rules(preds, rs)
        b = apply_entry_rules(preds, rs)
        pd.testing.assert_frame_equal(a, b)


class TestEdgeCases:
    def test_nan_prob_forced_flat(self) -> None:
        preds = pd.DataFrame({
            "ticker": ["A", "B"],
            "trade_date": [pd.to_datetime("2024-01-02").date()] * 2,
            "prob": [np.nan, 0.9],
        })
        out = apply_entry_rules(preds, RiskSettings())
        assert out.loc[0, "side"] == "flat"
        assert out.loc[1, "side"] == "long"

    def test_missing_is_benchmark_allows_all(self) -> None:
        preds = _preds().drop(columns=["is_benchmark"])
        out = apply_entry_rules(preds, RiskSettings())
        # SPY may now be selected because there is no benchmark marker
        assert out["selected"].sum() > 0

    def test_empty_input(self) -> None:
        preds = pd.DataFrame({
            "ticker": pd.Series([], dtype=str),
            "trade_date": pd.Series([], dtype="object"),
            "prob": pd.Series([], dtype=float),
        })
        out = apply_entry_rules(preds, RiskSettings())
        assert len(out) == 0
        assert "side" in out.columns
        assert "selected" in out.columns


class TestErrors:
    def test_missing_prob(self) -> None:
        preds = _preds().drop(columns=["prob"])
        with pytest.raises(ValueError, match="prob"):
            apply_entry_rules(preds, RiskSettings())

    def test_missing_ticker(self) -> None:
        preds = _preds().drop(columns=["ticker"])
        with pytest.raises(ValueError, match="ticker"):
            apply_entry_rules(preds, RiskSettings())

    def test_missing_trade_date(self) -> None:
        preds = _preds().drop(columns=["trade_date"])
        with pytest.raises(ValueError, match="trade_date"):
            apply_entry_rules(preds, RiskSettings())

    def test_unknown_method(self) -> None:
        preds = _preds()
        bad = RiskSettings(entry_method="threshold")
        object.__setattr__(bad, "entry_method", "not_a_method")
        with pytest.raises(ValueError, match="Unknown entry method"):
            apply_entry_rules(preds, bad)
