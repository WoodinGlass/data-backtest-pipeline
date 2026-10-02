{#
  Intermediate: one row per (series_id, vintage_date).

  Reduces stg_macro_series (24M rows, cumulative design) to ~280K rows
  by collapsing each vintage to its latest observation and the value at
  that observation. This table is the join key for int_macro_daily.

  Rationale: the PIT question "what was the value at trade_date T"
  decomposes into (a) which vintage was current at T, and (b) which
  observation inside that vintage was the latest known at T. Step (b)
  is almost always "the latest observation in the vintage" — the
  exception is projection series (see int_macro_daily for detail).

  See ADR 0009.
#}

{{ config(materialized='table') }}

select
    series_id,
    vintage_date,
    max(observation_date)                  as max_obs,
    max_by(value, observation_date)        as value_at_max_obs
from {{ ref('stg_macro_series') }}
group by 1, 2
