"""Unit tests for backtest.split. ADR 0014 §1, §2."""

from __future__ import annotations

from datetime import date
from itertools import pairwise

import pandas as pd
import pytest

from backtest.config import BacktestSettings
from backtest.split import Fold, _add_months, generate_folds

# ---------------------------------------------------------------------
# _add_months
# ---------------------------------------------------------------------


class TestAddMonths:
    def test_basic(self) -> None:
        assert _add_months(date(2024, 1, 15), 1) == date(2024, 2, 15)
        assert _add_months(date(2024, 1, 15), 12) == date(2025, 1, 15)
        assert _add_months(date(2024, 1, 15), -1) == date(2023, 12, 15)

    def test_clamp_day_of_month(self) -> None:
        assert _add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
        assert _add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)
        assert _add_months(date(2024, 3, 31), 1) == date(2024, 4, 30)

    def test_year_boundary(self) -> None:
        assert _add_months(date(2024, 12, 15), 1) == date(2025, 1, 15)
        assert _add_months(date(2024, 1, 15), -1) == date(2023, 12, 15)


# ---------------------------------------------------------------------
# Fold dataclass
# ---------------------------------------------------------------------


class TestFoldValidation:
    def test_rejects_train_start_after_train_end(self) -> None:
        with pytest.raises(ValueError):
            Fold(
                fold_id=0,
                train_start=date(2024, 2, 1),
                train_end=date(2024, 1, 1),
                test_start=date(2024, 3, 1),
                test_end=date(2024, 4, 1),
                n_train_days=10,
                n_test_days=10,
            )

    def test_rejects_no_gap(self) -> None:
        with pytest.raises(ValueError):
            Fold(
                fold_id=0,
                train_start=date(2024, 1, 1),
                train_end=date(2024, 3, 1),
                test_start=date(2024, 3, 1),
                test_end=date(2024, 4, 1),
                n_train_days=10,
                n_test_days=10,
            )


# ---------------------------------------------------------------------
# generate_folds
# ---------------------------------------------------------------------


@pytest.fixture
def trade_dates():
    return pd.bdate_range("2015-01-02", "2026-10-01").date.tolist()


class TestGenerateFolds:
    def test_produces_folds(self, trade_dates) -> None:
        folds = generate_folds(trade_dates, BacktestSettings())
        # ~35 folds over 2018-2026 at 3m step
        assert 30 <= len(folds) <= 40

    def test_first_fold_starts_around_2018(self, trade_dates) -> None:
        folds = generate_folds(trade_dates, BacktestSettings())
        assert folds[0].test_start.year == 2018
        assert folds[0].test_start.month == 1

    def test_no_test_window_overlap(self, trade_dates) -> None:
        folds = generate_folds(trade_dates, BacktestSettings())
        for a, b in pairwise(folds):
            assert a.test_end < b.test_start, (
                f"overlap: {a.fold_id} ends {a.test_end}, {b.fold_id} starts {b.test_start}"
            )

    def test_gap_of_six_trade_days(self, trade_dates) -> None:
        folds = generate_folds(trade_dates, BacktestSettings())
        for f in folds:
            between = [d for d in trade_dates if f.train_end < d < f.test_start]
            assert len(between) == 6, f"fold {f.fold_id}: gap is {len(between)}, expected 6"

    def test_ordered_by_test_start(self, trade_dates) -> None:
        folds = generate_folds(trade_dates, BacktestSettings())
        starts = [f.test_start for f in folds]
        assert starts == sorted(starts)

    def test_fold_ids_sequential(self, trade_dates) -> None:
        folds = generate_folds(trade_dates, BacktestSettings())
        assert [f.fold_id for f in folds] == list(range(len(folds)))

    def test_train_before_test(self, trade_dates) -> None:
        folds = generate_folds(trade_dates, BacktestSettings())
        for f in folds:
            assert f.train_start <= f.train_end
            assert f.train_end < f.test_start
            assert f.test_start <= f.test_end

    def test_date_bounds_respected(self, trade_dates) -> None:
        bs = BacktestSettings(
            first_test_start="2020-01-02",
            last_test_end="2022-12-31",
        )
        folds = generate_folds(trade_dates, bs)
        assert all(f.test_start >= date(2020, 1, 2) for f in folds)
        assert all(f.test_end <= date(2022, 12, 31) for f in folds)

    def test_empty_input_returns_empty(self) -> None:
        assert generate_folds([], BacktestSettings()) == []

    def test_deterministic(self, trade_dates) -> None:
        a = generate_folds(trade_dates, BacktestSettings())
        b = generate_folds(trade_dates, BacktestSettings())
        assert a == b

    def test_duplicate_dates_ignored(self, trade_dates) -> None:
        duped = trade_dates + trade_dates[:10]
        a = generate_folds(trade_dates, BacktestSettings())
        b = generate_folds(duped, BacktestSettings())
        assert len(a) == len(b)

    def test_train_size_is_approx_36_months(self, trade_dates) -> None:
        folds = generate_folds(trade_dates, BacktestSettings())
        # 36 months ~ 750-790 bdays
        for f in folds:
            assert 740 <= f.n_train_days <= 800, f.n_train_days

    def test_test_size_is_approx_3_months(self, trade_dates) -> None:
        folds = generate_folds(trade_dates, BacktestSettings())
        for f in folds:
            assert 55 <= f.n_test_days <= 70, f.n_test_days
