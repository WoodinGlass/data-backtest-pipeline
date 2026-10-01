{#
  Fact: daily log returns + forward labels, the model-ready table.

  Grain: one row per (ticker, trade_date).

  Column contract (see also int_returns):
    ── Features (known at end of t) ──────────────────────────
    close, adj_close, volume, log_return, sector, is_benchmark

    ── Labels (known at end of t+1) ──────────────────────────
    next_close, next_adj_close, next_log_return, next_return_positive

    ── Label timing metadata ─────────────────────────────────
    next_trade_date

  Anti-leakage: any model trained on this table must select features
  from the "features" block only. The `next_*` columns are the target.
  This is enforced in M4 by the feature builders, and tested in M5 by
  walk-forward evaluation.
#}

with returns as (

    select * from {{ ref('int_returns') }}

),

tickers as (

    select ticker, sector, is_benchmark, valid_from, valid_to
    from {{ ref('dim_tickers') }}

)

select
    r.ticker,
    r.trade_date,
    r.close,
    r.adj_close,
    r.volume,
    r.log_return,
    r.next_trade_date,
    r.next_close,
    r.next_adj_close,
    r.next_log_return,
    r.next_return_positive,
    t.sector,
    t.is_benchmark
from returns r
inner join tickers t
    on  r.ticker = t.ticker
    and r.trade_date between t.valid_from
                         and coalesce(t.valid_to, date '9999-12-31')
