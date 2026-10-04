"""Unit tests for backtest.baselines. ADR 0014 §5."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.baselines import (
    available_baselines,
    build_baseline,
    is_portfolio_baseline,
)
from backtest.config import BacktestSettings


def _features() -> pd.DataFrame:
    """3 dates x 5 stocks + SPY."""
    data = {
        "2024-01-02": {
            "A": (0.03, 0.90),
            "B": (0.01, 0.70),
            "C": (-0.02, 0.50),
            "D": (-0.05, 0.30),
            "E": (0.04, 0.85),
        },
        "2024-01-03": {
            "A": (-0.01, 0.55),
            "B": (0.02, 0.75),
            "C": (0.03, 0.80),
            "D": (-0.03, 0.40),
            "E": (0.05, 0.95),
        },
        "2024-01-04": {
            "A": (0.01, 0.60),
            "B": (-0.02, 0.45),
            "C": (0.04, 0.88),
            "D": (0.02, 0.72),
            "E": (-0.01, 0.50),
        },
    }
    rows = []
    for d, per_t in data.items():
        for t, (r20, rank) in per_t.items():
            rows.append(
                {
                    "ticker": t,
                    "trade_date": pd.to_datetime(d).date(),
                    "px_return_20d": r20,
                    "cs_percentile_rank_universe_20d": rank,
                }
            )
        rows.append(
            {
                "ticker": "SPY",
                "trade_date": pd.to_datetime(d).date(),
                "px_return_20d": 0.005,
                "cs_percentile_rank_universe_20d": 0.99,
            }
        )
    return pd.DataFrame(rows)


class TestRegistry:
    def test_expected_methods(self) -> None:
        assert available_baselines() == [
            "b0_naive",
            "b1_momentum",
            "b2_spy",
            "b3_top10",
        ]

    def test_portfolio_classification(self) -> None:
        assert is_portfolio_baseline("b0_naive") is True
        assert is_portfolio_baseline("b2_spy") is True
        assert is_portfolio_baseline("b1_momentum") is False
        assert is_portfolio_baseline("b3_top10") is False

    def test_portfolio_classification_from_df(self) -> None:
        assert is_portfolio_baseline(pd.DataFrame({"weight": [0.1]})) is True
        assert is_portfolio_baseline(pd.DataFrame({"prob": [0.5]})) is False

    def test_unknown_baseline_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown baseline"):
            build_baseline("does_not_exist", _features(), BacktestSettings())


class TestB0Naive:
    def test_all_rows_prob_05_weight_0(self) -> None:
        out = build_baseline("b0_naive", _features(), BacktestSettings())
        assert (out["prob"] == 0.5).all()
        assert (out["weight"] == 0.0).all()


class TestB1Momentum:
    def test_long_when_positive_return(self) -> None:
        out = build_baseline("b1_momentum", _features(), BacktestSettings())
        d1 = out[out["trade_date"] == pd.to_datetime("2024-01-02").date()]
        d1_map = d1.set_index("ticker")["prob"].to_dict()
        assert d1_map["A"] == 1.0  # +0.03
        assert d1_map["B"] == 1.0  # +0.01
        assert d1_map["C"] == 0.45  # -0.02
        assert d1_map["D"] == 0.45  # -0.05
        assert d1_map["E"] == 1.0  # +0.04

    def test_nan_return_is_flat(self) -> None:
        f = _features()
        f.loc[0, "px_return_20d"] = np.nan
        out = build_baseline("b1_momentum", f, BacktestSettings())
        assert out.loc[0, "prob"] == 0.45


class TestB2Spy:
    def test_one_row_per_date(self) -> None:
        out = build_baseline("b2_spy", _features(), BacktestSettings())
        assert len(out) == 3
        assert (out["ticker"] == "SPY").all()
        assert (out["weight"] == 1.0).all()

    def test_missing_benchmark_raises(self) -> None:
        f = _features()
        f = f[f["ticker"] != "SPY"]
        with pytest.raises(ValueError, match="benchmark"):
            build_baseline("b2_spy", f, BacktestSettings())


class TestB3TopN:
    def test_top3_by_rank_per_date(self) -> None:
        bs = BacktestSettings(top_n_baseline=3)
        out = build_baseline("b3_top10", _features(), bs)

        d1 = out[(out["trade_date"] == pd.to_datetime("2024-01-02").date()) & (out["prob"] == 1.0)]
        assert set(d1["ticker"]) == {"A", "B", "E"}

        d2 = out[(out["trade_date"] == pd.to_datetime("2024-01-03").date()) & (out["prob"] == 1.0)]
        assert set(d2["ticker"]) == {"B", "C", "E"}

    def test_benchmark_never_selected(self) -> None:
        bs = BacktestSettings(top_n_baseline=3)
        out = build_baseline("b3_top10", _features(), bs)
        spy = out[out["ticker"] == "SPY"]
        assert not (spy["prob"] == 1.0).any()

    def test_nan_rank_never_selected(self) -> None:
        f = _features()
        f.loc[f["ticker"] == "E", "cs_percentile_rank_universe_20d"] = np.nan
        bs = BacktestSettings(top_n_baseline=3)
        out = build_baseline("b3_top10", f, bs)
        e = out[out["ticker"] == "E"]
        assert (e["prob"] == 0.45).all()


class TestPurityAndErrors:
    def test_input_not_mutated(self) -> None:
        f = _features()
        f_bak = f.copy()
        for name in available_baselines():
            build_baseline(name, f, BacktestSettings())
        pd.testing.assert_frame_equal(f, f_bak)

    def test_missing_feature_column_raises(self) -> None:
        f = _features().drop(columns=["px_return_20d"])
        with pytest.raises(ValueError):
            build_baseline("b1_momentum", f, BacktestSettings())
