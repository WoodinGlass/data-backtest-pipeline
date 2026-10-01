{#
  Singular test: point-in-time boundaries on int_returns.

  Invariants:
    1. `next_trade_date`, when present, must be strictly greater than
       `trade_date` (calendar moves forward).
    2. Exactly one row per ticker has NULL `log_return` (the first bar).
    3. Exactly one row per ticker has NULL `next_log_return` (the last bar).
    4. `next_return_positive` NULL iff `next_log_return` NULL
       (belt-and-suspenders; also asserted as a column test).

  Each violation is returned as a row so dbt prints actionable detail.
#}

with

-- (1) forward date sanity
bad_forward_dates as (
    select
        'next_trade_date <= trade_date' as reason,
        ticker,
        trade_date,
        next_trade_date
    from {{ ref('int_returns') }}
    where next_trade_date is not null
      and next_trade_date <= trade_date
),

-- (2) log_return NULL count per ticker
bad_log_null as (
    select
        'log_return NULL count != 1 per ticker' as reason,
        ticker,
        count(*) filter (where log_return is null) as n_null,
        null::date as trade_date,
        null::date as next_trade_date
    from {{ ref('int_returns') }}
    group by ticker
    having count(*) filter (where log_return is null) <> 1
),

-- (3) next_log_return NULL count per ticker
bad_next_null as (
    select
        'next_log_return NULL count != 1 per ticker' as reason,
        ticker,
        count(*) filter (where next_log_return is null) as n_null,
        null::date as trade_date,
        null::date as next_trade_date
    from {{ ref('int_returns') }}
    group by ticker
    having count(*) filter (where next_log_return is null) <> 1
),

-- (4) label null mismatch
bad_label_null as (
    select
        'next_return_positive NULL mismatch' as reason,
        ticker,
        trade_date,
        next_trade_date
    from {{ ref('int_returns') }}
    where (next_return_positive is null) <> (next_log_return is null)
)

select reason, ticker, trade_date, next_trade_date from bad_forward_dates
union all
select reason, ticker, trade_date, next_trade_date from bad_log_null
union all
select reason, ticker, trade_date, next_trade_date from bad_next_null
union all
select reason, ticker, trade_date, next_trade_date from bad_label_null
