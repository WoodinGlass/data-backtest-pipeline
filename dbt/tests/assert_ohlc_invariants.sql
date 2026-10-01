{#
  Singular test: OHLC invariants must hold for every bar.

  Returns the offending rows (up to a reasonable limit) so that on
  failure, `dbt test` prints concrete evidence — dates and values —
  rather than a boolean.

  Invariants:
    - low  <= open  <= high
    - low  <= close <= high
    - all prices > 0
    - volume >= 0
#}

with violations as (

    select
        ticker,
        trade_date,
        open,
        high,
        low,
        close,
        volume,
        case
            when low  > open  then 'low > open'
            when low  > close then 'low > close'
            when open > high  then 'open > high'
            when close > high then 'close > high'
            when open <= 0    then 'open <= 0'
            when high <= 0    then 'high <= 0'
            when low  <= 0    then 'low <= 0'
            when close <= 0   then 'close <= 0'
            when volume < 0   then 'volume < 0'
        end as reason
    from {{ ref('stg_prices') }}

)

select * from violations
where reason is not null
