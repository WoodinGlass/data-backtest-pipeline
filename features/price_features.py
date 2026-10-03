"""Price-based point-in-time features.

Every feature here uses only data available **at the close of
trade_date t**. Because the backtest predicts the return from t to
t+1, using data available at t is correct PIT behaviour — no
additional shift is needed beyond the internal lags each feature
already carries (e.g. ``px_ret_5d`` uses ``adj_close`` at t and t-5).

Design (see ADR 0012):

- **Pure function.** ``add_price_features(df)`` takes a DataFrame and
  returns a new one with ``px_*`` columns added. No IO. No global
  state.
- **Groupby-safe.** All rolling and shift operations are partitioned
  by ``ticker``; features from one ticker never leak into another.
- **Boundary NaN.** The first ``window`` rows per ticker are NaN for
  window-based features. We do not fill them.

Inputs (columns required):

    ticker       str
    trade_date   date or datetime
    adj_close    float  (used for all returns and momentum)
    log_return   float  (from fct_returns_daily; NaN on first bar)

Outputs (columns added):

    px_ret_1d          = log_return at t
    px_ret_5d          = ln(adj_close_t / adj_close_{t-5})
    px_ret_20d         = ln(adj_close_t / adj_close_{t-20})
    px_vol_20d         = std(log_return, 20d) * sqrt(252)
    px_vol_60d         = std(log_return, 60d) * sqrt(252)
    px_rsi_14          = Wilder RSI(14) on adj_close
    px_mom_60d         = adj_close_t / adj_close_{t-60} - 1
    px_vol_ratio_5_20  = vol_5d / vol_20d (annualised)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from features.config import FeatureSettings, get_feature_settings

__all__ = [
    "add_price_features",
    "price_feature_columns",
]


def price_feature_columns(
    settings: FeatureSettings | None = None,
) -> tuple[str, ...]:
    """Return the ordered list of price feature column names.

    Derived from ``settings`` so that feature names always reflect the
    window lengths actually used. Tests that override windows get
    consistently named columns; production uses the defaults
    (``px_ret_5d``, ``px_vol_20d``, ``px_rsi_14``, ...).
    """
    s = settings or get_feature_settings()
    return (
        "px_ret_1d",
        f"px_ret_{s.window_short}d",
        f"px_ret_{s.window_medium}d",
        f"px_vol_{s.window_medium}d",
        f"px_vol_{s.window_long}d",
        f"px_rsi_{s.window_rsi}",
        f"px_mom_{s.window_long}d",
        f"px_vol_ratio_{s.window_short}_{s.window_medium}",
    )


def add_price_features(
    df: pd.DataFrame,
    *,
    settings: FeatureSettings | None = None,
) -> pd.DataFrame:
    """Return a copy of ``df`` with price features added.

    Args:
        df: Frame with at least ``ticker``, ``trade_date``,
            ``adj_close``, ``log_return``.
        settings: Optional FeatureSettings override.

    Returns:
        New DataFrame, sorted by (ticker, trade_date), with the
        ``px_*`` feature columns added.

    Raises:
        ValueError: if a required column is missing.
    """
    settings = settings or get_feature_settings()

    required = {"ticker", "trade_date", "adj_close", "log_return"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"add_price_features: missing columns {sorted(missing)}")

    out = df.sort_values(["ticker", "trade_date"]).copy().reset_index(drop=True)
    g = out.groupby("ticker", sort=False)
    ac = out["adj_close"]
    ann = np.sqrt(settings.trading_days_per_year)

    # px_ret_1d — already computed upstream (log_return)
    out["px_ret_1d"] = out["log_return"].astype("float64")

    # px_ret_Nd — log return over N trading days
    for n in (settings.window_short, settings.window_medium):
        out[f"px_ret_{n}d"] = np.log(ac / g["adj_close"].shift(n))

    # px_vol_Nd — annualized std of log returns over N days
    for n in (settings.window_medium, settings.window_long):
        out[f"px_vol_{n}d"] = (
            g["log_return"]
            .transform(lambda x, w=n: x.rolling(w, min_periods=w).std())
            .astype("float64")
            * ann
        )

    # Intermediate vol_5d, used only for the ratio.
    vol_5d = (
        g["log_return"]
        .transform(lambda x, w=settings.window_short: x.rolling(w, min_periods=w).std())
        .astype("float64")
        * ann
    )

    # px_vol_ratio_5_20
    vol_medium_col = f"px_vol_{settings.window_medium}d"
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = vol_5d / out[vol_medium_col]
    ratio_col = f"px_vol_ratio_{settings.window_short}_{settings.window_medium}"
    out[ratio_col] = ratio.replace([np.inf, -np.inf], np.nan)

    # px_mom_{window_long}d — total return over window_long trading days
    mom_col = f"px_mom_{settings.window_long}d"
    out[mom_col] = ac / g["adj_close"].shift(settings.window_long) - 1.0

    # px_rsi_{window_rsi} — Wilder's RSI on adj_close
    rsi_col = f"px_rsi_{settings.window_rsi}"
    out[rsi_col] = (
        g["adj_close"].transform(lambda x: _rsi(x, period=settings.window_rsi)).astype("float64")
    )

    return out


def _rsi(close: pd.Series[float], *, period: int = 14) -> pd.Series[float]:
    """Wilder's Relative Strength Index.

    RSI = 100 - 100 / (1 + RS), where RS = avg_gain / avg_loss and
    averages use Wilder's smoothing (EMA with alpha = 1/period,
    adjust=False).

    Returns a Series in [0, 100], NaN until ``period`` non-NaN
    deltas are available.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - 100.0 / (1.0 + rs)
    # If avg_loss is zero and avg_gain > 0, RSI = 100.
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    # If both are zero, RSI is undefined; leave NaN.
    both_zero = (avg_gain == 0.0) & (avg_loss == 0.0)
    return rsi.where(~both_zero, np.nan)
