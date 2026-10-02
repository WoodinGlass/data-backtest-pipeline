# Data Dictionary

> Populated incrementally as tables are added. One section per table.

## `raw.prices_daily` (planned, M1)

| Column | Type | Description | Notes |
|---|---|---|---|
| `source` | TEXT | Data source identifier (e.g. `yfinance`) | Part of idempotency key |
| `ticker` | TEXT | Ticker symbol (e.g. `AAPL`) | Part of idempotency key |
| `trade_date` | DATE | The bar's date (exchange tz, UTC-normalized) | Part of idempotency key |
| `ingested_at` | TIMESTAMPTZ | When we fetched it (UTC) | Set by us |
| `payload_hash` | TEXT | SHA-256 of canonical OHLCV payload | Part of idempotency key |
| `open` | DECIMAL | Open price | **Hashed** |
| `high` | DECIMAL | High price | **Hashed** |
| `low` | DECIMAL | Low price | **Hashed** |
| `close` | DECIMAL | Close price | **Hashed** |
| `adj_close` | DECIMAL | Adjusted close, computed by the vendor | **Not hashed** — see ADR 0006 |
| `volume` | BIGINT | Share volume | **Hashed**, non-negative |

### Which columns participate in the content hash?

The raw snapshot hash uses the primary (exchange-derived) fields only:

```
HASHED_COLUMNS = (open, high, low, close, volume)
PRICE_HASH_DECIMALS = 4
```

`adj_close` is **stored but not hashed**. It is a derived value that the
vendor (`yfinance`) recomputes on each request with minor float noise
(~3e-5 between fetches). Hashing it breaks idempotency: two snapshots
with identical OHLCV but different `adj_close` are semantically the same
bar and must be treated as such.

Adjusted prices will be recomputed from `close` + corporate actions in
the dbt layer (M2). See `docs/adr/0006-content-hash-excludes-adjusted-close.md`
for the full rationale and the evidence that motivated this decision.

## Staging / Intermediate / Marts

_To be added with M2._

Planned marts:

- `dim_tickers` — ticker metadata + point-in-time universe membership
- `fct_prices_daily` — cleaned, typed daily bars
- `fct_returns_daily` — log returns + labels (`next_return_positive`)
- `fct_predictions` — model predictions per `(ticker, date)`

## Raw macro layer (M3.5)

### `data/raw/macro/fred/<SERIES_ID>/<vintage>__<hash16>.parquet`

Each file is one vintage snapshot of one FRED series. Filename
encodes vintage date (human-readable) and content hash (dedup).

| Column | Type | Description | Notes |
|---|---|---|---|
| `series_id` | TEXT | FRED series id (e.g. `FEDFUNDS`) | Part of idempotency key |
| `observation_date` | DATE | The period the value describes | |
| `value` | DOUBLE | The observed value; NULL for missing (FRED's ".") | |
| `vintage_date` | DATE | FRED's vintage date (when this revision became current) | Part of idempotency key |

### `data/raw/macro/manifest.json`

| Field | Type | Description |
|---|---|---|
| `version` | INT | Manifest schema version |
| `series.<SERIES_ID>.snapshots` | ARRAY | List of `{hash, vintage_date, path, n_observations, n_non_null, first_date, last_date, written_at}` |
| `series.<SERIES_ID>.latest_vintage` | DATE | Newest vintage for quick lookup |

## Macro warehouse (M3.6)

### `staging.stg_macro_series`

One row per (series_id, observation_date, vintage_date). For latest-mode
series, `vintage_date` equals `observation_date` (PIT-effective).

| Column | Type | Description |
|---|---|---|
| `series_id` | VARCHAR | FRED series id |
| `observation_date` | DATE | Period the value describes |
| `vintage_date` | DATE | PIT-effective vintage date |
| `value` | DOUBLE | Observed value; NULL for FRED "." |

### `intermediate.int_macro_vintages`

One row per (series_id, vintage_date): latest observation and its value.

| Column | Type | Description |
|---|---|---|
| `series_id` | VARCHAR | FRED series id |
| `vintage_date` | DATE | Vintage date |
| `max_obs` | DATE | Latest observation_date in this vintage |
| `value_at_max_obs` | DOUBLE | Value at max_obs |

### `intermediate.int_macro_daily`

One row per (trade_date, series_id). PIT-correct: `vintage_date <=
trade_date` and `observation_date <= trade_date`.

| Column | Type | Description |
|---|---|---|
| `trade_date` | DATE | Trade date |
| `series_id` | VARCHAR | FRED series id |
| `vintage_date` | DATE | Vintage current at trade_date |
| `observation_date` | DATE | The period the value describes |
| `value` | DOUBLE | Macro value (may be NULL) |

### `marts.fct_macro_daily`

Wide: one row per trade_date, 140 `macro_<series_id_lowercase>` columns.
Column list from `macro_series_ids` dbt var.

## Macro warehouse (M3.6)

### `staging.stg_macro_series`

One row per (series_id, observation_date, vintage_date). For latest-mode
series, `vintage_date` equals `observation_date` (PIT-effective).

| Column | Type | Description |
|---|---|---|
| `series_id` | VARCHAR | FRED series id |
| `observation_date` | DATE | Period the value describes |
| `vintage_date` | DATE | PIT-effective vintage date |
| `value` | DOUBLE | Observed value; NULL for FRED "." |

### `intermediate.int_macro_vintages`

One row per (series_id, vintage_date): latest observation and its value.

| Column | Type | Description |
|---|---|---|
| `series_id` | VARCHAR | FRED series id |
| `vintage_date` | DATE | Vintage date |
| `max_obs` | DATE | Latest observation_date in this vintage |
| `value_at_max_obs` | DOUBLE | Value at max_obs |

### `intermediate.int_macro_daily`

One row per (trade_date, series_id). PIT-correct: `vintage_date <=
trade_date` and `observation_date <= trade_date`.

| Column | Type | Description |
|---|---|---|
| `trade_date` | DATE | Trade date |
| `series_id` | VARCHAR | FRED series id |
| `vintage_date` | DATE | Vintage current at trade_date |
| `observation_date` | DATE | The period the value describes |
| `value` | DOUBLE | Macro value (may be NULL) |

### `marts.fct_macro_daily`

Wide: one row per trade_date, 140 `macro_<series_id_lowercase>` columns.
Column list from `macro_series_ids` dbt var.

## Raw fundamental layer (M3.7)

### `data/raw/fundamentals/sec/<TICKER>/CIK#########__<hash16>.parquet`

One snapshot per ticker. Content-addressed by hash of the fact set.
No PIT enforcement at this layer (see ADR 0010 implementation notes).

| Column | Type | Description |
|---|---|---|
| `ticker` | VARCHAR | Ticker symbol (uppercase) |
| `cik` | BIGINT | SEC Central Index Key |
| `namespace` | VARCHAR | `us-gaap`, `dei`, `ifrs-full`, `ffd`, ... |
| `tag` | VARCHAR | XBRL tag name, e.g. `Revenues` |
| `unit` | VARCHAR | `USD`, `shares`, `pure`, ... |
| `period_start` | DATE | Period start; NULL for point-in-time facts |
| `period_end` | DATE | Period end |
| `filed` | DATE | Date the fact first appeared in a filing |
| `form` | VARCHAR | `10-K`, `10-Q`, `8-K`, ... |
| `fiscal_year` | INT | SEC's fiscal year label, if present |
| `fiscal_period` | VARCHAR | `Q1`, `Q2`, `Q3`, `Q4`, `FY` |
| `frame` | VARCHAR | SEC calendar frame, if present |
| `value` | DOUBLE | Observed value; NULL for discontinued items |

### `data/raw/fundamentals/manifest.json`

| Field | Type | Description |
|---|---|---|
| `version` | INT | Manifest schema version |
| `tickers.<TICKER>.snapshots` | ARRAY | List of `{hash, cik, path, n_facts, n_non_null, n_tags, first_filed, last_filed, written_at}` |
| `tickers.<TICKER>.latest_hash` | TEXT | Newest hash for quick lookup |
