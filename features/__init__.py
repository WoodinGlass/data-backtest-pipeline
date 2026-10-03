"""Point-in-time feature engineering.

Builds the wide feature table consumed by the walk-forward backtest
(M5). Features read from three marts in the warehouse:

    marts.fct_returns_daily      (prices + labels)
    marts.fct_macro_daily        (macro, vintage-aware)
    intermediate.int_fundamentals_pit  (fundamentals, filing-date PIT)

and write a single versioned Parquet:

    data/features/<FEATURE_VERSION>/features_daily.parquet

Design (see ADR 0012):

- **Pure functions.** Every feature builder takes a DataFrame and
  returns a DataFrame. No IO. No global state.
- **Prefix naming.** ``px_`` prices, ``cs_`` cross-sectional,
  ``mkt_`` market context, ``mc_`` macro, ``fd_`` fundamental.
- **Anti-leakage by test.** For every feature family, a test replaces
  future rows with garbage, recomputes, and asserts the past is
  unchanged.
- **Versioned output.** ``FEATURE_VERSION`` bumps produce a new
  directory; old versions remain.
"""

__all__: list[str] = []
