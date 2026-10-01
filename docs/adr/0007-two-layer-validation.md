# 7. Two-layer OHLCV validation

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

Every snapshot written to the raw layer must be well-formed: prices
strictly positive, `low <= open, close <= high`, no NaN, sorted unique
dates, non-negative volume. Two places could enforce this:

1. **`RawPriceEvent`** (Pydantic model, one row at a time).
2. **`validate_ohlcv_frame`** (vectorized pandas, one DataFrame at a
   time).

A universe of 32 tickers × 10 years ≈ 80,000 rows per backtest. Running
a Pydantic model per row would dominate runtime for a pipeline whose
purpose is *other* work. But Pydantic is the right tool at other
boundaries (API endpoints, ad-hoc corrections, tests).

We chose to maintain **both**, with a clear division of labor.

## Decision

Two validation layers, same invariants, different scopes:

| Layer | Tool | Used at | Cost (10y × 32 ticker) |
|---|---|---|---|
| Row | `RawPriceEvent` (Pydantic) | API boundaries, tests, single-row fixes | ~seconds per 1k rows |
| Frame | `validate_ohlcv_frame` (pandas) | Every `RawStore.write_snapshot` | <100 ms total |

**Failure is always a hard fail.** `validate_ohlcv_frame` raises
`DataQualityError` (a `ValueError` subclass). The pipeline propagates it
up; the per-ticker wrapper in `pipeline.py` isolates the failure so that
one bad ticker does not abort the run — the exit code reflects it.

## Consequences

**Positive**

- The raw layer cannot silently accept corrupt data.
- Validation cost is negligible in the hot path (<1% of runtime).
- Existing unit tests that manufactured invalid frames to test *other*
  behavior broke loudly when the validator was added — a useful signal
  that the tests themselves were constructing impossible data.

**Negative / trade-offs**

- Two implementations of the same invariants must be kept in sync. We
  mitigate by keeping the invariant set small and both call sites next
  to their tests.
- When M3 adds Pandera/Great Expectations, this validation becomes the
  first gate and Pandera the second. That layering is intentional; the
  raw-layer check is cheap and runs *before* any file is written.

**Recorded lesson**

> Validate at the boundary, not per item. Vectorize the hot path;
> reserve per-item models for edges where the volume is small and the
> clarity is worth the cost.
