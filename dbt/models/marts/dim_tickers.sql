{#
  Dimension: one row per (ticker, validity interval).

  For the MVP, every ticker is valid from 2015-01-01 with no end date.
  The schema is designed for point-in-time membership (ADR 0005): when
  a ticker is delisted or removed from the universe, close its interval
  by setting `valid_to` and insert a new row for the next interval.

  Consumers (fct_prices_daily, fct_returns_daily) join on the interval
  [valid_from, valid_to] rather than assuming today's membership.
#}

with metadata as (

    select * from {{ ref('ticker_metadata') }}

)

select
    cast(ticker       as varchar) as ticker,
    cast(sector       as varchar) as sector,
    cast(is_benchmark as boolean) as is_benchmark,
    cast(valid_from   as date)    as valid_from,
    -- Empty string in the CSV becomes NULL, meaning "still valid".
    nullif(cast(valid_to as varchar), '')::date as valid_to
from metadata
