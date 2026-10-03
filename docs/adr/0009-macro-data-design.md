# 9. Macro data design: FRED + ALFRED (vintage-aware)

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

The feature engineering layer (M4) needs macro-economic context to
enrich daily equity predictions. Initial candidates were three series
(policy rate, nonfarm payroll, CPI). On review, the scope was expanded
to a curated set of ~150 FRED series spanning ten categories.

Three requirements shaped the design:

1. **Point-in-time (PIT) correctness.** A backtest on date T must only
   see macro data a trader could have actually seen on T. FRED's
   default view returns the *current* (revised) value of every
   observation, which is look-ahead bias. We use ALFRED (Archival
   FRED), which stores the vintage of each series on each release.

2. **Idempotency.** Rerunning ingestion must not duplicate data. The
   stable identity of an observation is
   `(series_id, observation_date, vintage_date)`.

3. **Reproducibility.** A reviewer cloning the repo and running
   `make ingest-macro` must obtain identical raw data, modulo revisions
   FRED itself makes to its archive (which we treat as new snapshots,
   same policy as ADR 0006).

## Decision

### Sources

- **FRED** for metadata (series titles, units, frequency).
- **ALFRED** for vintage-aware observations.

Both are served by the same API endpoint (fred.stlouisfed.org),
with ALFRED accessed via `realtime_start` / `realtime_end` params.

### Curation

~150 series, curated in `config/macro_series.yml`, grouped by category:

| Category        | Count | Example series                              |
|-----------------|-------|---------------------------------------------|
| Monetary policy | ~10   | FEDFUNDS, DFF, DFEDTARU, DFEDTARL, WALCL    |
| Rates           | ~20   | DGS10, DGS2, DGS30, T10Y2Y, T10Y3M, DPRIME  |
| Inflation       | ~20   | CPIAUCSL, CPILFESL, PCEPI, PCEPILFE, PPIACO |
| Employment      | ~20   | PAYEMS, UNRATE, ICSA, CCSA, JTSJOL, U6RATE  |
| Growth          | ~15   | GDP, GDPC1, INDPRO, RSAFS, HOUST, UMCSENT   |
| Money & credit  | ~15   | M1SL, M2SL, BUSLOANS, TOTALSL, WALCL        |
| Housing         | ~10   | CSUSHPINSA, EXHOSLUSM495S, MSPUS            |
| Energy          | ~10   | DCOILWTICO, GASREGW, DHHNGSP                |
| Sentiment       | ~10   | UMCSENT, VIXCLS, NFCICREDIT                 |
| International   | ~10   | DEXUSEU, DEXJPUS, DEXCHUS                   |

Selection criteria: authoritative publisher, monthly or higher
frequency, coverage of 2015–present, non-redundant with other series.

### Storage

Immutable raw Parquet layer, mirroring the prices design (M1):

```
data/raw/macro/
├── manifest.json
└── fred/
    ├── FEDFUNDS/
    │   ├── <hash>.parquet
    │   └── ...
    └── ...
```

Each snapshot is one `(series, vintage)` pair. Content hash is
computed over `(series_id, observation_dates[], values[], vintage_date)`,
rounded to a precision coarser than FRED's own rounding noise.

### PIT join

`fct_macro_daily` is built by forward-filling the *vintage that was
current on each trade date*:

- For each trade date in the backtest window, pick the ALFRED vintage
  with `vintage_date <= trade_date`, value at `observation_date`.
- Forward-fill to daily frequency (macro series are monthly/quarterly).

Enforced by a Pandera check: no value may originate from a vintage
later than the trade date.

### Warehouse layers

- `staging.stg_macro_series` — long format `(series_id, obs_date,
  vintage_date, value)`.
- `intermediate.int_macro_daily` — one row per `(series_id, trade_date)`,
  PIT-correct vintage.
- `marts.fct_macro_daily` — wide format, one column per series,
  joined to the daily trade calendar.

## Consequences

**Positive**

- Backtests are PIT-correct with respect to macro data. This is a
  meaningful bar that most retail backtests do not clear.
- ~150 series gives the model rich context across the business cycle.
- Design mirrors M1 (prices), so operators learn one pattern.

**Negative / trade-offs**

- Ingestion size grows ~5–10× vs final-value-only. Each monthly series
  over 10 years × several vintages produces many rows.
- ALFRED API is slower per-series than FRED. Full ingestion ~5–10 min.
- FRED occasionally corrects its own archive. We treat corrections as
  new snapshots, consistent with ADR 0006.

**Explicitly rejected**

- *FRED final values only.* Introduces look-ahead bias. Rejected.
- *Small macro set (3–10 series).* Rejected in favour of a curated 150
  that spans the cycle. Ingestion cost is bounded and one-time.
- *Direct BLS/BEA scraping.* More brittle than FRED, and FRED is the
  canonical aggregation.

## Related

- ADR 0006 — content hash excludes vendor noise.
- ADR 0010 — fundamental data design (SEC EDGAR XBRL).
- M1 — the ingestion pattern this ADR extends.

## Implementation notes (added 2026-10-02)

Three real-world findings from building M3.6:

### Latest-mode series need a PIT-effective vintage date

FRED returns a *single* vintage_date for daily series (DGS10, VIXCLS,
DFF, ...) — the date of the most recent refresh (e.g. 2026-09-30).
Using this directly in a PIT join makes those 28 series invisible to
any backtest before that date.

Fix: in `stg_macro_series`, for latest-mode series only (see
`macro_latest_mode_ids` var), set `vintage_date := observation_date`.
These series are never revised; the value for day D is published on D
(same-day for treasury rates, next business day at most for VIX). This
is the correct conservative choice for t+1 equity prediction using
data known at end of t.

### Naive PIT join is O(trade_dates x staging_rows)

The first version of `int_macro_daily` cross-joined 2,954 trade_dates
against 24M staging rows and took ~19 minutes. The final design
decomposes:

1. `int_macro_vintages` collapses staging to one row per
   (series_id, vintage_date) with its latest observation and value.
   ~280K rows.
2. `int_macro_daily` does an ASOF join of trade_dates × distinct
   series to find the current vintage, then a filtered second scan of
   staging for the (rare) projection series case (GDPPOT, IORB, IOER).

Total: ~67s. 17× faster.

### Append-only raw layer needs retention

The raw layer is append-only by design (ADR 0006). Re-ingesting with a
wider window leaves the older snapshot on disk, and downstream glob
reads duplicate data. Cleanup scripts (`scripts/cleanup_*_raw.py`)
keep the widest snapshot per ticker/series. This is a stopgap; a
retention policy is M10 work.

### Known limitation: number of vintages for daily series

Full-vintage ingestion for daily series (DGS*, DEX*, DFF, etc.) would
require 5000+ vintage dates per series, exceeding FRED's per-request
limit. Latest-mode is the correct workaround *because those series are
not revised*. For future work, a hybrid strategy (full vintage for
monthly/quarterly, latest for daily) is already what we implement.

## Latest-mode expansion (added 2025-10-03)

### Context

Full-vintage ingestion for daily FRED series is prohibitively
expensive. During the M3.6 build we found that 30 daily series
(mostly DEX* exchange rates and daily commodity spot prices) were
responsible for the bulk of the raw layer:

| Group                  | Series | Vintages each | Rows each |
|------------------------|--------|---------------|-----------|
| Exchange rates (DEX*)  | 8      | ~613          | ~1.9M     |
| Trade-weighted USD     | 3      | ~400          | ~1.2M     |
| Energy spot (DCOIL*,..)| 4      | ~605          | ~1.8M     |
| SOFR + IOER            | 2      | ~1600         | ~2M       |

Aggregate: **~17 series, ~10,500 files, ~200 MB**, producing
**~17.6M rows** in `stg_macro_series` — 75% of the staging table.

### Decision

Extend `config/macro_series_latest_only.yml` from 28 to 45 series.
The 17 added series are daily AND non-revised:

- Exchange rates (DEXSDUS, DEXCHUS, DEXUSUK, DEXMXUS, DEXUSEU,
  DEXCAUS, DEXJPUS, DEXINUS).
- Trade-weighted USD indices (DTWEXBGS, DTWEXEMEGS, DTWEXAFEGS).
- Daily spot commodity prices (DCOILBRENTEU, DCOILWTICO,
  DDFUELUSGULF, DHHNGSP).
- Daily overnight rates (SOFR, IOER).

All are published once per business day, not subsequently revised
to a degree that matters for equity-direction prediction.

### Implementation

1. Add the 17 series to `macro_series_latest_only.yml`.
2. Delete all raw snapshots for those 17 series (11,731 files,
   199.5 MB) and their manifest entries.
3. Re-ingest in latest mode: each series now produces exactly one
   snapshot covering the full history.
4. Rebuild `stg_macro_series` and downstream models.

### Consequence

| Metric                       | Before    | After    | Δ      |
|------------------------------|-----------|----------|--------|
| Raw macro files              | ~36,000   | ~24,500  | −32%   |
| Raw macro size on disk       | 320 MB    | 120 MB   | −62%   |
| `stg_macro_series` rows      | 23.7M     | 5.9M     | −75%   |
| Macro full ingest time       | ~115 min  | ~15 min  | −87%   |
| `int_macro_daily` rows       | 394K      | 350K     | −11%   |

The `int_macro_daily` decrease is expected and correct: a handful
of early-2015 trade dates now have no historical value for the
promoted series because no observation exists at that date, and the
PIT rule forbids forward-filling from the future. We would rather
report NULL than fabricate a value.

### Future work

`config/macro_series_latest_only.yml` will keep growing as we
identify more daily non-revised series. The principle is simple:

> If a series has no meaningful revision history and is published
> at daily frequency, use latest mode. Reserve full vintage for
> economic series that FRED actually revises (CPI, PAYEMS, GDP,
> rates that get restated, etc.).
