"""Limits, stops, and drawdown halt. ADR 0013 §3.

Contract
--------

``apply_all_limits`` is a **pure function**::

    apply_all_limits(target_weights, prices, settings) -> DataFrame

Input:
    target_weights — long format, one row per (ticker, trade_date):
        ticker      str
        trade_date  date  (sorted ascending)
        weight      float (target weight BEFORE limits)

    prices — long format, one row per (ticker, trade_date):
        ticker      str
        trade_date  date
        close       float (> 0)

Output:
    Same rows as ``target_weights`` in the SAME order, with four
    additional columns:
        weight_before_limits  float   (input ``weight``, unchanged)
        weight                float   (post stop-loss, post DD scaling)
        stop_out              bool    (True iff forced exit at this row)
        dd_state              str     ('normal' | 'derisk' | 'halt')

Rules applied, in order, per trade_date (chronological single pass):

1. **Update NAV** using previous day's effective weights and today's
   realized asset returns. Track peak; compute drawdown.

2. **DD halt** — if ``drawdown <= settings.dd_halt_trigger``:
   set all weights to 0 for this date and every subsequent date.

3. **DD derisk** — else if ``drawdown <= settings.dd_derisk_trigger``:
   multiply today's target weights by ``settings.dd_derisk_factor``.

4. **Stop loss** — for each ticker with a continuing position, compute
   ``close_today / entry_price - 1``. If ``<= settings.stop_loss_pct``:
   zero the position, mark ``stop_out = True``, start a cooldown of
   ``settings.stop_loss_cooldown_days``. ``entry_price`` is set on the
   transition 0 -> non-zero and cleared when weight returns to 0.

5. **Cooldown** — while in cooldown, target weight is forced to 0.

Purity
------

All state (entry prices, peak NAV, cooldown expiry) is local to the
call. The function does not mutate its inputs. Same input -> same
output.

See ADR 0013 for rationale and parameter meanings.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from risk.config import RiskSettings

# ---------------------------------------------------------------------
# Standalone: cap per-name positions (stateless)
# ---------------------------------------------------------------------


def cap_position_limits(
    weights: pd.DataFrame,
    settings: RiskSettings | None = None,
) -> pd.DataFrame:
    """Enforce ``settings.kelly_cap`` and non-negativity per row.

    Stateless. Useful as a defensive check before/after other rules.
    Returns a copy with the ``weight`` column clipped.
    """
    if settings is None:
        settings = RiskSettings()
    out = weights.copy()
    cap = settings.kelly_cap
    if not settings.allow_short:
        out["weight"] = out["weight"].clip(lower=0.0, upper=cap)
    else:
        out["weight"] = out["weight"].clip(lower=-cap, upper=cap)
    return out


# ---------------------------------------------------------------------
# Internal helpers (pure)
# ---------------------------------------------------------------------


def _validate_inputs(
    target_weights: pd.DataFrame,
    prices: pd.DataFrame,
) -> None:
    req_w = {"ticker", "trade_date", "weight"}
    req_p = {"ticker", "trade_date", "close"}
    miss_w = req_w - set(target_weights.columns)
    miss_p = req_p - set(prices.columns)
    if miss_w:
        raise ValueError(
            f"target_weights missing columns: {sorted(miss_w)}. Required: {sorted(req_w)}."
        )
    if miss_p:
        raise ValueError(f"prices missing columns: {sorted(miss_p)}. Required: {sorted(req_p)}.")
    if (prices["close"] <= 0).any():
        raise ValueError("prices['close'] must be positive.")


def _wide_to_long(df: pd.DataFrame, value_name: str) -> pd.DataFrame:
    """Wide matrix (index=date, cols=ticker) -> long (date, ticker, value).

    ``DataFrame.stack()`` returns an unnamed Series; we set ``.name``
    explicitly so the reset_index column is predictable.
    """
    s = df.stack()
    s.name = value_name
    return s.reset_index().rename(
        columns={"level_0": "trade_date", "level_1": "ticker"},
    )


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------


def apply_all_limits(
    target_weights: pd.DataFrame,
    prices: pd.DataFrame,
    settings: RiskSettings | None = None,
) -> pd.DataFrame:
    """Apply stop loss, cooldown, and drawdown rules chronologically.

    See module docstring for the contract. Pure: does not mutate
    inputs. Deterministic.
    """
    if settings is None:
        settings = RiskSettings()

    _validate_inputs(target_weights, prices)

    # Handle empty input early
    if target_weights.empty:
        out = target_weights.copy()
        out["weight_before_limits"] = out["weight"].astype(float)
        out["stop_out"] = pd.Series([], index=out.index, dtype=bool)
        out["dd_state"] = pd.Series([], index=out.index, dtype=object)
        return out

    # Build wide matrices
    px = prices.pivot(
        index="trade_date",
        columns="ticker",
        values="close",
    ).sort_index()
    tickers = list(px.columns)

    w_raw = (
        target_weights.pivot(index="trade_date", columns="ticker", values="weight")
        .reindex(columns=tickers)
        .sort_index()
        .fillna(0.0)
    )

    # Align dates: only dates present in BOTH weights and prices
    dates = w_raw.index.intersection(px.index).sort_values()
    if len(dates) == 0:
        raise ValueError("No overlapping trade_date between target_weights and prices.")
    w_raw = w_raw.loc[dates]
    px = px.loc[dates]

    # Asset returns
    ret = px.pct_change()

    # Working state (all local to this call)
    w_adj = w_raw.copy()
    stop_out = pd.DataFrame(False, index=dates, columns=tickers)
    dd_state = pd.Series("normal", index=dates, dtype=object)

    entry_price: dict[str, float] = {}
    cooldown_until: dict[str, pd.Timestamp] = {}
    nav = 1.0
    peak = 1.0
    halted = False

    for i, d in enumerate(dates):
        # --- 1. Portfolio return from yesterday's effective weights ---
        if i == 0 or halted:
            port_ret = 0.0
        else:
            prev_w = w_adj.iloc[i - 1]
            r_today = ret.loc[d]
            port_ret = float((prev_w * r_today).sum(skipna=True))
            if not np.isfinite(port_ret):
                port_ret = 0.0

        nav *= 1.0 + port_ret
        peak = max(peak, nav)
        dd = (nav / peak) - 1.0 if peak > 0 else 0.0

        # --- 2. DD halt ---
        if halted or dd <= settings.dd_halt_trigger:
            halted = True
            dd_state.iloc[i] = "halt"
            w_adj.iloc[i] = 0.0
            continue

        # --- 3. DD derisk ---
        if dd <= settings.dd_derisk_trigger:
            dd_state.iloc[i] = "derisk"
            scale = settings.dd_derisk_factor
        else:
            scale = 1.0

        # --- 4. Per-ticker stop loss + cooldown ---
        row_target = w_raw.iloc[i] * scale
        row_final = row_target.copy()
        px_today = px.loc[d]

        for t in tickers:
            # Cooldown check
            if t in cooldown_until and d <= cooldown_until[t]:
                row_final[t] = 0.0
                continue

            target_w = row_target[t]
            in_position_now = target_w != 0.0
            was_in_position = t in entry_price

            if not in_position_now:
                # Target says flat: clear entry price if we had one.
                if was_in_position:
                    del entry_price[t]
                continue

            price_now = px_today[t]
            if not np.isfinite(price_now) or price_now <= 0:
                # Can't price -> cannot trade
                row_final[t] = 0.0
                if was_in_position:
                    del entry_price[t]
                continue

            if not was_in_position:
                # New entry
                entry_price[t] = float(price_now)
                continue

            # Continuing: check stop loss
            ret_since_entry = (price_now / entry_price[t]) - 1.0
            if ret_since_entry <= settings.stop_loss_pct:
                row_final[t] = 0.0
                stop_out.iloc[i, stop_out.columns.get_loc(t)] = True
                del entry_price[t]
                cd_end_idx = min(
                    i + settings.stop_loss_cooldown_days,
                    len(dates) - 1,
                )
                cooldown_until[t] = dates[cd_end_idx]

        w_adj.iloc[i] = row_final

    # --- 5. Reshape wide -> long, then restore original row order ---
    w_long = _wide_to_long(w_adj, "weight")
    wb_long = _wide_to_long(w_raw, "weight_before_limits")
    so_long = _wide_to_long(stop_out, "stop_out")

    dd_long = dd_state.rename("dd_state").reset_index().rename(columns={"index": "trade_date"})

    long = (
        w_long.merge(wb_long, on=["trade_date", "ticker"], how="left")
        .merge(so_long, on=["trade_date", "ticker"], how="left")
        .merge(dd_long, on="trade_date", how="left")
    )

    # Restore original row order from target_weights
    # Restore original ROW ORDER (positional), not label order.
    # Using positional range makes the function transparent:
    # whatever row order the caller passes in, they get it back.
    restore = target_weights[["ticker", "trade_date"]].copy()
    restore["_pos"] = range(len(target_weights))
    out = restore.merge(long, on=["ticker", "trade_date"], how="left")
    out = out.set_index("_pos").sort_index()

    out = out[
        [
            "ticker",
            "trade_date",
            "weight_before_limits",
            "weight",
            "stop_out",
            "dd_state",
        ]
    ]

    # Dtype hygiene
    out["stop_out"] = out["stop_out"].fillna(False).astype(bool)
    out["dd_state"] = out["dd_state"].fillna("normal")

    # Final defensive cap
    if not settings.allow_short:
        out["weight"] = out["weight"].clip(lower=0.0, upper=settings.kelly_cap)
    else:
        out["weight"] = out["weight"].clip(
            lower=-settings.kelly_cap,
            upper=settings.kelly_cap,
        )

    return out


__all__ = ["apply_all_limits", "cap_position_limits"]
