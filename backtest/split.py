"""Walk-forward fold generation with purge + embargo.

Contract: docs/adr/0014-walk-forward-methodology.md §1, §2.

Given the list of unique trade dates and a BacktestSettings, produce
a sequence of folds. Each fold has a non-overlapping test window.

Layout per fold (all dates are trading dates):

    [------ train -------][purge][embargo][--- test ---]
    train_start    train_end   ^      ^   test_start  test_end
                              drop   drop
                            1 row  5 rows

- train_window: rolling, `train_window_months` calendar months.
- test_window: `test_window_months` calendar months.
- step: `step_months` calendar months (test windows do not overlap).
- purge: `purge_days` trading rows dropped from train tail.
- embargo: `embargo_days` more rows dropped from train tail.

Folds with fewer than `min_train_days` usable rows are skipped.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from backtest.config import BacktestSettings

# ---------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Fold:
    """One walk-forward fold. All dates are inclusive trade dates."""

    fold_id: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    n_train_days: int
    n_test_days: int

    def __post_init__(self) -> None:
        if self.train_start > self.train_end:
            raise ValueError(f"fold {self.fold_id}: train_start > train_end")
        if self.test_start > self.test_end:
            raise ValueError(f"fold {self.fold_id}: test_start > test_end")
        if self.train_end >= self.test_start:
            raise ValueError(
                f"fold {self.fold_id}: train_end >= test_start (no gap between train and test)"
            )


# ---------------------------------------------------------------------
# Month arithmetic (avoid extra dependency)
# ---------------------------------------------------------------------


def _add_months(d: date, months: int) -> date:
    """Add `months` calendar months to `d`, clamping day-of-month."""
    m0 = d.month - 1 + months
    y = d.year + m0 // 12
    m = m0 % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


# ---------------------------------------------------------------------
# Fold generation
# ---------------------------------------------------------------------


def generate_folds(
    trade_dates: list[date],
    settings: BacktestSettings | None = None,
    *,
    min_train_days: int = 100,
    min_test_days: int = 20,
) -> list[Fold]:
    """Generate walk-forward folds from the given trade dates.

    Parameters
    ----------
    trade_dates
        All available trade dates. Duplicates are ignored; order does
        not matter.
    settings
        BacktestSettings. Defaults are used if None.
    min_train_days
        Minimum usable train rows (after purge+embargo) required for a
        fold to be emitted. Guards against degenerate folds near the
        start of the series.
    min_test_days
        Minimum test rows required for a fold to be emitted.

    Returns
    -------
    list[Fold]
        Sorted by test_start. Test windows do not overlap.
    """
    if settings is None:
        settings = BacktestSettings()

    dates = sorted(set(trade_dates))
    if not dates:
        return []

    # Determine the first test_start date.
    if settings.first_test_start is not None:
        first_test_start = settings.first_test_start
    else:
        # Earliest date such that train_window_months of history exist.
        earliest_train_start = dates[0]
        first_test_start = _add_months(earliest_train_start, settings.train_window_months)

    # Determine the last date to consider (exclusive end for loops).
    last_test_end = settings.last_test_end if settings.last_test_end is not None else dates[-1]

    folds: list[Fold] = []
    test_start = first_test_start
    fold_id = 0

    while True:
        # Stop as soon as the test window has advanced past the
        # caller-supplied upper bound. Without this guard, the loop
        # below can spin forever when last_test_end truncates
        # test_dates to empty but test_start can't advance further.
        if test_start > last_test_end:
            break

        # Test window: [test_start, test_end] inclusive.
        test_end_target = _add_months(test_start, settings.test_window_months)
        # test_end is the last trade date strictly before the target
        # (avoids overlap with the next fold's start).
        # We use "<= target" and rely on step >= test_window to
        # guarantee non-overlap.
        test_dates = [d for d in dates if test_start <= d < test_end_target]
        # Respect the caller-supplied upper bound on the
        # test window. Without this, the last fold can
        # extend past last_test_end.
        test_dates = [d for d in test_dates if d <= last_test_end]
        if not test_dates:
            # Advance until the next trade date or bail.
            nxt = [d for d in dates if d >= test_start]
            if not nxt:
                break
            test_start = nxt[0]
            continue

        test_end = test_dates[-1]

        if test_start > last_test_end:
            break

        # Train window: [train_window_start, test_start), then drop
        # purge + embargo from the tail.
        train_window_start = _add_months(test_start, -settings.train_window_months)
        candidates = [d for d in dates if train_window_start <= d < test_start]

        gap = settings.purge_days + settings.embargo_days
        if gap > 0:
            train_usable = candidates[:-gap] if len(candidates) > gap else []
        else:
            train_usable = candidates

        if len(train_usable) >= min_train_days and len(test_dates) >= min_test_days:
            folds.append(
                Fold(
                    fold_id=fold_id,
                    train_start=train_usable[0],
                    train_end=train_usable[-1],
                    test_start=test_start,
                    test_end=test_end,
                    n_train_days=len(train_usable),
                    n_test_days=len(test_dates),
                )
            )
            fold_id += 1

        # Advance by step months.
        next_start = _add_months(test_start, settings.step_months)

        # Skip forward to the first trade date >= next_start.
        nxt = [d for d in dates if d >= next_start]
        if not nxt:
            break
        new_start = nxt[0]
        if new_start <= test_start:
            # Safety against pathological settings.
            break
        test_start = new_start

    return folds


__all__ = ["Fold", "generate_folds"]
