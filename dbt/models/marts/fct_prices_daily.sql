{#
  Fact: daily OHLCV bars enriched with dimension attributes.

  Grain: one row per (ticker, trade_date).

  Point-in-time membership: the join condition uses the interval
  [valid_from, valid_to] so that a backtest on date T only sees tickers
  that were actually members on T. See ADR 0005.
#}

with prices as (

    select * from {{ ref('stg_prices') }}

),

tickers as (

    select ticker, sector, is_benchmark, valid_from, valid_to
    from {{ ref('dim_tickers') }}

)

select
    p.ticker,
    p.trade_date,
    p.open,
    p.high,
    p.low,
    p.close,
    p.adj_close,
    p.volume,
    t.sector,
    t.is_benchmark
from prices p
inner join tickers t
    on  p.ticker = t.ticker
    and p.trade_date between t.valid_from
                         and coalesce(t.valid_to, date '9999-12-31')
