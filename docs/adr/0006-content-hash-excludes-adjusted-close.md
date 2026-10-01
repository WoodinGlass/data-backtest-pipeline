# 6. Content hash excludes ``adj_close``

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

The raw layer uses a **content hash** to decide whether an incoming
snapshot is new or a duplicate of one we already have. Two facts
collided during integration testing of M1:

1. We want **semantic idempotency**: fetching the same AAPL bars twice
   should not create two files on disk.
2. `yfinance` (Yahoo Finance) recomputes the ``Adj Close`` field on every
   request using a cumulative dividend/split factor that is refreshed
   independently. That recomputation introduces tiny floating-point
   differences between otherwise-identical fetches.

Concrete evidence, captured during the M1 integration run:

```
AAPL 2024-01-02 (two consecutive fetches)
  open       identical
  high       identical
  low        identical
  close      identical
  volume     identical
  adj_close  183.40403747558594  vs  183.4040069580078   (diff ≈ 3e-5)
```

Across 21 bars, **17 of 21** had a different ``adj_close`` while every
other field was byte-identical. The hash, which included ``adj_close``,
changed every run. Run #2 wrote new files instead of skipping them.
Idempotency — a headline property of M1 — silently did not hold.

## Decision

The content hash includes **only the exchange-derived OHLCV fields**:

```
HASHED_COLUMNS = ("open", "high", "low", "close", "volume")
```

``adj_close`` is still **stored** in every snapshot (we are honest about
what the vendor returned), but it does **not** participate in the hash.

Additionally, price values are rounded to a precision coarser than the
observed vendor noise before hashing:

```
PRICE_HASH_DECIMALS = 4   # ~1e-4 — below real revisions, above vendor noise
```

## Consequences

**Positive**

- Idempotency works as advertised: refetching the same bars is a no-op.
- The raw layer is **reproducible** across runs, machines, and CI.
- ``adj_close`` will be recomputed from ``close`` + corporate actions in
  the dbt layer (M2), which is the correct place for derived data.

**Negative / trade-offs**

- A genuine upstream change to ``adj_close`` alone (no OHLCV change) will
  not create a new snapshot. We accept this: ``adj_close`` is a derived
  quantity that we own downstream, not a primary input.
- The hash version was bumped (``raw_store:v2`` → ``raw_store:v3``). Any
  pre-existing snapshots computed under v2 are not comparable; the M1
  layer is append-only anyway, so old hashes simply remain as history.

**Lesson recorded**

> Vendor-derived columns are not primary data. Hash the primary data.
> Compute the derived columns yourself.

This is a small but instructive case of integration testing catching a
bug that no unit test would have found: every unit test asserted that
*fetching twice returns the same DataFrame* — which it did not, because
the upstream itself is non-deterministic at the last float bit.
