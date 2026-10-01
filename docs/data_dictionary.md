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
| `open` | DECIMAL | Open price | Adjusted for splits/dividends per source |
| `high` | DECIMAL | High price | Same |
| `low` | DECIMAL | Low price | Same |
| `close` | DECIMAL | Close price | Same |
| `adj_close` | DECIMAL | Adjusted close | Same |
| `volume` | BIGINT | Share volume | Non-negative |

## Staging / Intermediate / Marts

_To be added with M2._

Planned marts:

- `dim_tickers` — ticker metadata + point-in-time universe membership
- `fct_prices_daily` — cleaned, typed daily bars
- `fct_returns_daily` — log returns + labels (`next_return_positive`)
- `fct_predictions` — model predictions per `(ticker, date)`
