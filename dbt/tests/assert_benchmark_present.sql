{#
  Singular test: exactly one benchmark ticker must exist, and every
  trade_date must have exactly one benchmark row in fct_prices_daily.

  Rationale: SPY is the comparison baseline for the backtest (M5). If
  it is missing on any date, or duplicated, backtest metrics become
  meaningless. We fail loudly rather than let the backtest silently
  produce a wrong benchmark.
#}

with benchmark_count as (

    select count(*) as n_benchmarks
    from {{ ref('dim_tickers') }}
    where is_benchmark

),

-- dates where SPY is missing
missing_benchmark as (

    select
        d.trade_date,
        'missing benchmark row' as reason
    from (select distinct trade_date from {{ ref('fct_prices_daily') }}) d
    left join (
        select trade_date from {{ ref('fct_prices_daily') }} where is_benchmark
    ) b using (trade_date)
    where b.trade_date is null

),

-- duplicate benchmark rows per date
dup_benchmark as (

    select
        trade_date,
        'multiple benchmark rows' as reason
    from {{ ref('fct_prices_daily') }}
    where is_benchmark
    group by trade_date
    having count(*) > 1

),

-- exactly one benchmark ticker in dim
bad_dim as (

    select
        null::date as trade_date,
        'dim_tickers has ' || cast(n_benchmarks as varchar)
            || ' benchmark(s), expected exactly 1' as reason
    from benchmark_count
    where n_benchmarks <> 1

)

select trade_date, reason from missing_benchmark
union all
select trade_date, reason from dup_benchmark
union all
select trade_date, reason from bad_dim
