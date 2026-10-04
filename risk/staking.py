"""Staking rules: turn probabilities into target position weights.

Contract (ADR 0013 §1 and §2)
-----------------------------

Each staking rule is a **pure function** with the signature::

    fn(signals: pd.DataFrame, settings: RiskSettings) -> pd.Series[float]

``signals`` has one row per (ticker, trade_date) that has already been
selected for a position by the entry rule (see ``risk/entry.py``).
Required columns:

    ticker          str
    trade_date      date
    prob            float, P(return_{t+1} > 0), in [0, 1]
    realized_vol    float, annualized, e.g. 0.22 for 22%
                    (used by vol_target; may be NaN otherwise)

Returns a Series aligned to ``signals.index`` with target weights.
Weight 0 means flat. Weights are capped at ``settings.kelly_cap`` per
name. Negative weights only occur if ``settings.allow_short`` is True,
which is out of scope for v1 (ADR 0013 §4).

Rules implemented
-----------------

- ``kelly``              — quarter-Kelly: f = k * (2p - 1), cap.
- ``fixed_fractional``   — every name gets ``settings.fixed_fractional``.
- ``equal_weight``       — 1/N per trade_date, capped.
- ``vol_target``         — target_vol / realized_vol, capped.

Registry
--------

``_STAKING_REGISTRY`` maps method name -> callable. New methods are
added by defining a function and registering it; the dispatcher
``compute_weights`` never changes. See ADR 0013 §8.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from risk.config import RiskSettings


# ---------------------------------------------------------------------
# Pure staking functions
# ---------------------------------------------------------------------

def _size_kelly(
    signals: pd.DataFrame, settings: RiskSettings,
) -> pd.Series:
    """Quarter-Kelly: f = k * (2p - 1), clipped to [0, cap].

    ADR 0013 §1. Uses even-odds binary approximation; long-only v1.
    """
    p = signals["prob"].astype(float)
    f_star = 2.0 * p - 1.0                      # raw Kelly fraction
    f = settings.kelly_fraction * f_star        # fractional Kelly
    f = f.clip(lower=0.0, upper=settings.kelly_cap)
    return f


def _size_fixed_fractional(
    signals: pd.DataFrame, settings: RiskSettings,
) -> pd.Series:
    """Constant size per name, capped. ADR 0013 §1 (alternatives)."""
    size = min(settings.fixed_fractional, settings.kelly_cap)
    return pd.Series(size, index=signals.index, dtype=float)


def _size_equal_weight(
    signals: pd.DataFrame, settings: RiskSettings,
) -> pd.Series:
    """1/N per trade_date, capped at kelly_cap. ADR 0013 §1 (alternatives).

    N is the count of eligible names **on the same trade_date**.
    If 1/N exceeds the cap, the cap wins (rest of NAV stays in cash).
    """
    n_per_date = signals.groupby("trade_date")["ticker"].transform("count")
    raw_w = 1.0 / n_per_date.astype(float)
    w = np.minimum(raw_w, settings.kelly_cap)
    return pd.Series(w, index=signals.index, dtype=float)


def _size_vol_target(
    signals: pd.DataFrame, settings: RiskSettings,
) -> pd.Series:
    """Position size = min(1, target_vol / realized_vol), scaled by cap.

    ADR 0013 §2. ``scale`` only shrinks; no leverage-up in calm regimes.
    Missing/zero realized_vol falls back to full cap (conservative: we
    do not fabricate a low-vol reading to inflate size).
    """
    rv = signals["realized_vol"].astype(float)
    target = settings.target_vol_annual
    with np.errstate(divide="ignore", invalid="ignore"):
        scale = target / rv
    scale = scale.where(rv > 0, np.nan)
    scale = scale.clip(lower=0.0, upper=1.0)
    # If rv is missing or zero, use 1.0 (full cap, no leverage-up).
    scale = scale.fillna(1.0)
    w = scale * settings.kelly_cap
    return pd.Series(w.values, index=signals.index, dtype=float)


# ---------------------------------------------------------------------
# Registry (ADR 0013 §8)
# ---------------------------------------------------------------------

_STAKING_REGISTRY: dict[str, Callable[[pd.DataFrame, RiskSettings], pd.Series]] = {
    "kelly": _size_kelly,
    "fixed_fractional": _size_fixed_fractional,
    "equal_weight": _size_equal_weight,
    "vol_target": _size_vol_target,
}


def register_staking(
    name: str,
) -> Callable[
    [Callable[[pd.DataFrame, RiskSettings], pd.Series]],
    Callable[[pd.DataFrame, RiskSettings], pd.Series],
]:
    """Decorator: register a new staking rule under ``name``."""
    def _decorator(
        fn: Callable[[pd.DataFrame, RiskSettings], pd.Series],
    ) -> Callable[[pd.DataFrame, RiskSettings], pd.Series]:
        if name in _STAKING_REGISTRY:
            raise ValueError(f"Staking rule '{name}' is already registered.")
        _STAKING_REGISTRY[name] = fn
        return fn
    return _decorator


def available_staking_methods() -> list[str]:
    """Return the sorted list of registered staking method names."""
    return sorted(_STAKING_REGISTRY)


# ---------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------

_REQUIRED_SIGNAL_COLUMNS = ("ticker", "trade_date", "prob")


def compute_weights(
    signals: pd.DataFrame, settings: RiskSettings | None = None,
) -> pd.Series:
    """Apply the staking rule selected by ``settings.staking_method``.

    Pure: same input -> same output. Does not mutate ``signals``.

    Parameters
    ----------
    signals
        One row per (ticker, trade_date). Must contain columns
        ``ticker``, ``trade_date``, ``prob``. ``realized_vol`` is
        required only by the ``vol_target`` rule.
    settings
        Risk framework settings. If ``None``, ``RiskSettings()`` is
        constructed with environment overrides applied.

    Returns
    -------
    pd.Series[float]
        Target weights aligned to ``signals.index``, each in
        ``[0, settings.kelly_cap]`` for v1 (long-only).

    Raises
    ------
    ValueError
        If ``signals`` is missing a required column, or if the
        selected staking method is unknown.
    """
    if settings is None:
        settings = RiskSettings()

    method = settings.staking_method
    if method not in _STAKING_REGISTRY:
        raise ValueError(
            f"Unknown staking method: {method!r}. "
            f"Available: {available_staking_methods()}."
        )

    missing = [c for c in _REQUIRED_SIGNAL_COLUMNS if c not in signals.columns]
    if missing:
        raise ValueError(
            f"signals is missing required columns: {missing}. "
            f"Required: {list(_REQUIRED_SIGNAL_COLUMNS)}."
        )

    if method == "vol_target" and "realized_vol" not in signals.columns:
        raise ValueError(
            "Staking method 'vol_target' requires column 'realized_vol'."
        )

    if signals.empty:
        return pd.Series([], index=signals.index, dtype=float)

    w = _STAKING_REGISTRY[method](signals, settings)
    # Final safety: enforce non-negative for v1, cap at kelly_cap.
    if not settings.allow_short:
        w = w.clip(lower=0.0)
    w = w.clip(upper=settings.kelly_cap)
    return w.astype(float)


__all__ = [
    "compute_weights",
    "available_staking_methods",
    "register_staking",
]
