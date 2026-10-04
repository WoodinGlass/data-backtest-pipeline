# 12. Feature engineering architecture

- **Status:** Accepted
- **Date:** 2025-10-03

## Context

M4 turns prices, macro, and fundamentals into a wide feature table for
the walk-forward backtest (M5). Three questions must be answered
before any code:

1. **Where do features live?** dbt (SQL) or Python or both?
2. **How are they stored?** Warehouse table, Parquet, or computed on
   demand?
3. **How do we guarantee no look-ahead?** Feature engineering is the
   highest-risk place for leakage in the whole pipeline.

The constraints:

- **Pure functions are testable.** A feature builder that reads from
  disk cannot be unit-tested; a feature builder that takes a DataFrame
  and returns a DataFrame can.
- **The pipeline is reproducible.** The same raw layer plus the same
  feature code must produce byte-identical feature Parquet.
- **Anti-leakage is a first-class concern.** We want a test that
  would catch a bug like "RSI uses t+1 close" without needing to
  eyeball every formula.
- **Features evolve.** v1 might be returns + vol; v2 adds RSI +
  fundamentals. Old versions must remain intact for reproducibility.

## Decision

### Features are computed in Python

Not in dbt. Reasons:

- Rolling-window features (RSI, momentum) and cross-sectional ranks
  are awkward in SQL and easy in pandas.
- Anti-leakage tests need to shuffle the input DataFrame and compare
  outputs; that is a Python test, not a dbt test.
- A single Python module (per feature family) can be unit-tested in
  isolation without a warehouse.

dbt still owns the marts: `fct_returns_daily`, `fct_macro_daily`,
`int_fundamentals_pit`. Features read from those marts and write to a
Parquet artifact. A thin dbt model reads the artifact back into the
warehouse so backtest (M5) can query it like any other table.

### Features are stored as versioned Parquet

Layout::

    data/features/
    ├── manifest.json
    └── v1/
        └── features_daily.parquet

The version comes from ``FEATURE_VERSION`` in ``features/config.py``.
Bumping the version creates a new directory; old versions remain.
This mirrors the raw layer's content-addressing discipline (ADR 0006,
ADR 0009, ADR 0010): the data is immutable, versions accumulate, and
the manifest records what each version contains.

### Feature naming

Every feature name carries a prefix that identifies its source family:

| Prefix | Source        | Examples                                    |
|--------|---------------|---------------------------------------------|
| `px_`  | prices        | `px_ret_1d`, `px_vol_20d`, `px_rsi_14`      |
| `cs_`  | cross-section | `cs_sector_rank_20d`, `cs_market_rank_20d`  |
| `mkt_` | market context| `mkt_beta_60d`, `mkt_corr_60d`              |
| `mc_`  | macro         | `mc_fedfunds`, `mc_cpi_yoy`, `mc_payems_yoy`|
| `fd_`  | fundamental   | `fd_net_margin`, `fd_roe`, `fd_rev_growth`  |

The prefix is not cosmetic: it lets a reviewer see at a glance where
a feature came from, and it groups features in the schema for
downstream feature selection.

### Feature builders are pure functions

Each builder has the signature::

    def add_price_features(df: pd.DataFrame) -> pd.DataFrame:
        ...

Input: a DataFrame with raw prices indexed by ``(ticker, trade_date)``.
Output: the same DataFrame with feature columns added.
No IO. No global state. No randomness (except documented seeds).

This makes every builder:

- **Testable**: pass a tiny DataFrame, assert the output columns.
- **Composable**: assembler runs builders in order.
- **Reversible**: nothing is mutated in place; each builder returns
  a new frame.

### Anti-leakage is enforced by test, not by convention

For every feature family, a test constructs a frame, computes
features, then **replaces all rows after time T with garbage**,
recomputes, and asserts that the feature values at time T are
identical. If a feature secretly reads future data, this test fails.

This is the pattern used in M3's PIT checks, extended to every
feature.

## Consequences

**Positive**

- Feature logic is pure, testable, and easy to reason about.
- Anti-leakage is verified mechanically, not by review.
- Versions accumulate; v1 remains available after v2 lands.
- The dbt layer stays lean: prices + macro + fundamentals + one thin
  features view.

**Negative / trade-offs**

- Features are not visible in dbt's DAG. A reviewer who opens the
  dbt project sees marts but not feature builders. We mitigate with
  a thin `marts.fct_features_daily` model and clear documentation.
- The pipeline gains a step (Python) between warehouse and backtest.
  `make features` must run before `make backtest`.
- Versioning is manual: bumping `FEATURE_VERSION` is a developer
  decision, not automatic. We accept this: automatic versioning
  hides the fact that features changed.

**Explicitly rejected**

- *Features in dbt.* Rolling windows and cross-sectional ranks are
  painful in SQL; anti-leakage tests are awkward in dbt; RSI is
  ugly.
- *Features computed on demand inside the backtest.* Would make
  backtest runs slow and non-reproducible across code changes.
- *Feature store* (Feast, Tecton, etc.). Overkill for a project
  with one model and a fixed universe. The Parquet artifact is the
  feature store.

## Related

- ADR 0006 — content addressing for raw prices.
- ADR 0009 — macro vintage and PIT.
- ADR 0010 — SEC filing-date PIT.
- ADR 0011 — memory strategy for large models.
- ADR 0013 (planned, M4.5) — risk framework (parameter + formulas).

## Implementation notes (added 2026-10-04)

### Final v1 feature set (23 features)

| Prefix | Count | Names |
|---|---|---|
| `px_` | 8 | `ret_1d`, `ret_5d`, `ret_20d`, `vol_20d`, `vol_60d`, `rsi_14`, `mom_60d`, `vol_ratio_5_20` |
| `cs_` | 2 | `sector_rank_20d`, `market_rank_20d` |
| `mkt_` | 2 | `beta_60d`, `corr_60d` |
| `mc_` | 6 | `fedfunds`, `dgs10`, `dgs2`, `cpi_yoy`, `payems_yoy`, `unrate` |
| `fd_` | 5 | `net_margin`, `roe`, `capex_intensity`, `rev_growth_yoy`, `employees` |

Columns in `fct_features_daily` (or `features_daily.parquet`) are
these 23 features plus 3 metadata (`ticker`, `trade_date`, `sector`)
and 5 label/context columns (`adj_close`, `log_return`,
`next_log_return`, `next_return_positive`, `is_benchmark`). Total:
31 columns, 94,528 rows (32 tickers × ~2,954 trade dates).

### Feature column names are functions of settings

An early version of `price_features.py` exposed a module-level
constant `PRICE_FEATURE_COLUMNS = ("px_ret_5d", ...)`. This broke as
soon as tests overrode window lengths (settings with
`window_short=3` produced `px_ret_3d`, not `px_ret_5d`). Feature
names are now produced by `price_feature_columns(settings)`,
`macro_feature_columns(settings)`, etc. The assembler calls these to
determine the canonical column order.

### Helper columns must not leak

`add_market_context_features` originally joined the benchmark's
`log_return` as `benchmark_log_return` and left it in the output.
That is an internal helper for beta/correlation, not a feature. The
builder now drops it before returning, and the assembler applies a
defensive filter that removes any column not in
`(metadata | known features | preserved labels)`. A regression test
asserts no extra columns appear.

### Float-precision guards

Two invariants were violated by float arithmetic in practice:

- `corr` computed via `rolling().corr()` occasionally returned
  `1 + 2e-16`. Clipped to `[-1, 1]` in the builder.
- Ratios with tiny denominators explode. `fd_net_margin` and
  `fd_roe` are clipped to `[-5, 5]`; `fd_capex_intensity` to
  `[0, 2]`; `fd_rev_growth_yoy` to `[-1, 5]`. These are bounds, not
  invariants; the caller can choose to treat clipped values as NaN
  if the bound feels too generous.

### Anti-leakage testing pattern

Every feature family has the same test shape:

1. Compute features on clean input, take value at time T.
2. Poison all rows after T (replace with garbage, e.g. random large
   numbers or fully shuffled values).
3. Recompute features.
4. Assert the value at T is unchanged.

If a feature secretly reads future data — a centred rolling window,
a reversed shift, a mis-aligned join — this test fails. It is the
mechanical equivalent of "no look-ahead" for the whole layer.

### Where features sit in the DAG

The pipeline is now::

    raw → dbt staging/intermediate/marts → features (Python) → backtest

`fct_features_daily` (dbt) reads the feature Parquet back into the
warehouse so the backtest (M5) can consume it as a normal table.
Features are not in the dbt DAG; they are an artifact of the Python
layer that the dbt layer happens to read. This is the trade-off
recorded in the Decision section above.
