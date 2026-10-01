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
