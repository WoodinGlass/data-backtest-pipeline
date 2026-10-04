"""Entry rules: turn model probabilities into long / flat selections.

Contract (ADR 0013 §4 and §8)
-----------------------------

Each entry rule is a **pure function** with the signature::

    fn(predictions: pd.DataFrame, settings: RiskSettings) -> pd.DataFrame

``predictions`` has one row per (ticker, trade_date) with at least:

    ticker          str
    trade_date      date
    prob            float, P(return_{t+1} > 0), in [0, 1]

Optional column:

    is_benchmark    bool (default False if absent)

The function returns a new DataFrame with the same index and all input
columns preserved, plus two added columns:

    side        'long' | 'flat'   (never 'short' unless allow_short=True)
    selected    bool              (True iff side == 'long')

Conventions
-----------

- All decisions use only data available at the close of day ``t``.
  This matches the feature PIT contract (ADR 0012) and the label
  definition (``next_return_positive`` = return from ``t`` to ``t+1``).
- Benchmark rows (``is_benchmark == True``) are **forced flat** and
  excluded from cross-sectional ranking. The benchmark is used for
  evaluation, not trading.
- v1 is long-only (ADR 0013 §4). ``side == 'short'`` never occurs
  unless ``settings.allow_short`` is True, which is out of scope.

Rules implemented
-----------------

- ``threshold``       — long if ``prob > settings.entry_prob_threshold``.
- ``top_n``           — per trade_date, top N by prob go long.
- ``cross_sectional`` — per trade_date, long if prob is above the
                        cross-sectional median.

Registry
--------

``_ENTRY_REGISTRY`` maps method name -> callable. New methods are
added by defining a function and registering it; the dispatcher
``apply_entry_rules`` never changes. See ADR 0013 §8.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from risk.config import RiskSettings

# ---------------------------------------------------------------------
# Pure entry-rule functions
# ---------------------------------------------------------------------


def _select_threshold(
    predictions: pd.DataFrame,
    settings: RiskSettings,
) -> pd.DataFrame:
    """Long when prob > threshold; flat otherwise. ADR 0013 §4.

    This is the default method. It is independent per (ticker, date):
    the number of concurrent positions is not bounded by the rule
    itself (limits are handled in risk/limits.py).
    """
    p = predictions["prob"].astype(float)
    is_long = p > settings.entry_prob_threshold
    return _apply_side(predictions, is_long, settings)


def _select_top_n(
    predictions: pd.DataFrame,
    settings: RiskSettings,
) -> pd.DataFrame:
    """Top-N names by prob per trade_date go long. ADR 0013 §8.

    Ties are broken by ticker (stable). Benchmark rows never enter
    the ranking. If a date has fewer than N eligible names, all
    eligible names are selected.
    """
    n = settings.entry_top_n
    eligible = _eligible_mask(predictions)

    # Rank by prob descending, ties broken by ticker asc.
    df = predictions.loc[eligible, ["trade_date", "ticker", "prob"]].copy()
    df["_rank"] = df.groupby("trade_date")["prob"].rank(method="first", ascending=False)
    selected_idx = df.index[df["_rank"] <= n]

    is_long = pd.Series(False, index=predictions.index)
    is_long.loc[selected_idx] = True
    return _apply_side(predictions, is_long, settings)


def _select_cross_sectional(
    predictions: pd.DataFrame,
    settings: RiskSettings,
) -> pd.DataFrame:
    """Long if prob is above the cross-sectional median per date.

    ADR 0013 §8. Benchmark rows are excluded from the median
    computation and forced flat. The median is computed per
    trade_date over eligible (non-benchmark) rows only.
    """
    eligible = _eligible_mask(predictions)

    # Per-date median over eligible rows
    med = predictions.loc[eligible].groupby("trade_date")["prob"].transform("median")

    p = predictions["prob"].astype(float)
    is_long = pd.Series(False, index=predictions.index)
    above = (p > med.reindex(predictions.index)).fillna(False)
    is_long.loc[eligible] = above.loc[eligible]

    return _apply_side(predictions, is_long, settings)


# ---------------------------------------------------------------------
# Helpers (pure)
# ---------------------------------------------------------------------


def _eligible_mask(predictions: pd.DataFrame) -> pd.Series:
    """True for rows that can be traded (non-benchmark, prob not NaN)."""
    if "is_benchmark" in predictions.columns:
        not_bench = ~predictions["is_benchmark"].astype(bool)
    else:
        not_bench = pd.Series(True, index=predictions.index)
    prob_ok = predictions["prob"].notna()
    return not_bench & prob_ok


def _apply_side(
    predictions: pd.DataFrame,
    is_long: pd.Series,
    settings: RiskSettings,
) -> pd.DataFrame:
    """Build the output DataFrame with ``side`` and ``selected``.

    Pure: does not mutate ``predictions``. Preserves index and all
    input columns. Order of output rows is unchanged.
    """
    out = predictions.copy()
    is_long = is_long.reindex(out.index, fill_value=False)

    side = pd.Series("flat", index=out.index, dtype=object)
    side.loc[is_long] = "long"
    if settings.allow_short:
        # Reserved for M5.5. Currently a no-op because no rule produces
        # a short signal, but the dispatcher honors the flag.
        pass
    out["side"] = side
    out["selected"] = side.eq("long")
    return out


# ---------------------------------------------------------------------
# Registry (ADR 0013 §8)
# ---------------------------------------------------------------------

_ENTRY_REGISTRY: dict[str, Callable[[pd.DataFrame, RiskSettings], pd.DataFrame]] = {
    "threshold": _select_threshold,
    "top_n": _select_top_n,
    "cross_sectional": _select_cross_sectional,
}


def register_entry(
    name: str,
) -> Callable[
    [Callable[[pd.DataFrame, RiskSettings], pd.DataFrame]],
    Callable[[pd.DataFrame, RiskSettings], pd.DataFrame],
]:
    """Decorator: register a new entry rule under ``name``."""

    def _decorator(
        fn: Callable[[pd.DataFrame, RiskSettings], pd.DataFrame],
    ) -> Callable[[pd.DataFrame, RiskSettings], pd.DataFrame]:
        if name in _ENTRY_REGISTRY:
            raise ValueError(f"Entry rule '{name}' is already registered.")
        _ENTRY_REGISTRY[name] = fn
        return fn

    return _decorator


def available_entry_methods() -> list[str]:
    """Return the sorted list of registered entry method names."""
    return sorted(_ENTRY_REGISTRY)


# ---------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------

_REQUIRED_PREDICTION_COLUMNS = ("ticker", "trade_date", "prob")


def apply_entry_rules(
    predictions: pd.DataFrame,
    settings: RiskSettings | None = None,
) -> pd.DataFrame:
    """Apply the entry rule selected by ``settings.entry_method``.

    Pure: same input -> same output. Does not mutate ``predictions``.

    Parameters
    ----------
    predictions
        One row per (ticker, trade_date). Must contain ``ticker``,
        ``trade_date``, ``prob``. Optional ``is_benchmark`` (bool).
    settings
        Risk framework settings. If ``None``, ``RiskSettings()`` is
        constructed with environment overrides applied.

    Returns
    -------
    pd.DataFrame
        Copy of ``predictions`` with two added columns:
        ``side`` in {'long', 'flat'} and ``selected`` (bool).

    Raises
    ------
    ValueError
        If ``predictions`` is missing a required column, or if the
        selected entry method is unknown.
    """
    if settings is None:
        settings = RiskSettings()

    method = settings.entry_method
    if method not in _ENTRY_REGISTRY:
        raise ValueError(
            f"Unknown entry method: {method!r}. Available: {available_entry_methods()}."
        )

    missing = [c for c in _REQUIRED_PREDICTION_COLUMNS if c not in predictions.columns]
    if missing:
        raise ValueError(
            f"predictions is missing required columns: {missing}. "
            f"Required: {list(_REQUIRED_PREDICTION_COLUMNS)}."
        )

    if predictions.empty:
        out = predictions.copy()
        out["side"] = pd.Series([], index=out.index, dtype=object)
        out["selected"] = pd.Series([], index=out.index, dtype=bool)
        return out

    return _ENTRY_REGISTRY[method](predictions, settings)


__all__ = [
    "apply_entry_rules",
    "available_entry_methods",
    "register_entry",
]
