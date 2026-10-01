# 5. Universe is defined as-of date, not as-of today

- **Status:** Accepted
- **Date:** 2025-01-01

## Context

Selecting "today's S&P 500 members" and running a backtest over the last
5 years produces **survivorship bias**: the companies that were removed for
poor performance are silently excluded, inflating returns.

## Decision

The universe is stored as a point-in-time table with
`(ticker, valid_from, valid_to)`. Backtests join against this table using the
backtest date, not `today()`.

Delisted tickers are **retained** in the raw layer and remain eligible in
backtests for the period during which they were in the index.

## Consequences

- dbt models must join universe membership on
  `trade_date BETWEEN valid_from AND valid_to`.
- Raw ingestion is allowed to receive delisted tickers that no longer return
  data from `yfinance`.
- A regression test asserts that a synthetic delisting in the middle of the
  backtest window does not silently drop the ticker's earlier rows.
