{#
  Staging: raw prices -> typed, renamed, one row per (ticker, trade_date).

  What this does:
    - casts raw columns to their canonical types
    - renames `date` -> `trade_date` (avoid reserved-word footguns
      downstream and make intent explicit)
    - passes through both `close` (raw) and `adj_close` (vendor-derived)

  What this does NOT do:
    - no deduplication (raw layer is already content-addressed)
    - no filtering by ticker or date (that belongs in marts)
    - no joins (no other source yet)
#}

with source as (

    select * from {{ source('raw', 'prices') }}

),

renamed as (

    select
        cast(ticker       as varchar) as ticker,
        cast(date         as date)    as trade_date,
        cast(open         as double)  as open,
        cast(high         as double)  as high,
        cast(low          as double)  as low,
        cast(close        as double)  as close,
        cast(adj_close    as double)  as adj_close,
        cast(volume       as bigint)  as volume
    from source

)

select * from renamed
