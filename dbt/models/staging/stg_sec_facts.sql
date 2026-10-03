{#
  Staging: raw SEC facts -> typed, renamed, 1:1 with raw.

  No tag filtering and no PIT filtering here; those happen in
  intermediate. See ADR 0010.
#}

with source as (

    select * from {{ source('raw', 'sec') }}

),

renamed as (

    select
        cast(ticker         as varchar) as ticker,
        cast(cik            as bigint)  as cik,
        cast(namespace      as varchar) as namespace,
        cast(tag            as varchar) as tag,
        cast(unit           as varchar) as unit,

        cast(period_start   as date)    as period_start,
        cast(period_end     as date)    as period_end,
        cast(filed          as date)    as filed,

        cast(form           as varchar) as form,
        cast(fiscal_year    as integer) as fiscal_year,
        cast(fiscal_period  as varchar) as fiscal_period,
        cast(frame          as varchar) as frame,
        cast(value          as double)  as value

    from source

)

select * from renamed
