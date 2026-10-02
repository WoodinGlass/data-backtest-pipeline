{#
  Intermediate: macro values on trading days, point-in-time correct.

  For each (series_id, trade_date):
      1. V* = max(vintage_date) where vintage_date <= trade_date
      2. From V*, the latest observation_date <= trade_date
      3. Take its value

  Performance design
  ------------------
  The previous version joined trade_dates × vintages × 24M staging
  rows in one query, taking ~19 minutes. This version decomposes:

    - int_macro_vintages collapses stg to ~280K (series, vintage) rows.
    - We cross-join trade_dates × distinct series to get ~414K pairs.
    - ASOF JOIN finds the latest vintage <= trade_date per pair (fast
      in DuckDB, purpose-built for "latest row <= key").
    - The value is almost always `value_at_max_obs` from that vintage.
      Only projection series (GDPPOT, IORB, IOER) need a second scan
      because their vintage contains future observation_dates; we
      filter those keys down to a few thousand before the second scan.

  Grain: one row per (trade_date, series_id).
#}

{{ config(materialized='table') }}

with trade_dates as (

    select distinct trade_date
    from {{ ref('fct_returns_daily') }}

),

vintages as (

    select * from {{ ref('int_macro_vintages') }}

),

-- Cartesian of (trade_date, series) is small: ~2950 × 140 = ~414K.
pairs as (

    select td.trade_date, s.series_id
    from trade_dates td
    cross join (select distinct series_id from vintages) s

),

-- ASOF: for each (trade_date, series), the latest vintage whose
-- vintage_date is <= trade_date.
pit_vintage as (

    select
        l.trade_date,
        l.series_id,
        v.vintage_date,
        v.max_obs,
        v.value_at_max_obs
    from pairs l
    asof join vintages v
        on l.series_id = v.series_id
       and l.trade_date >= v.vintage_date

),

-- Common case: the vintage's latest observation is <= trade_date,
-- so value_at_max_obs is the correct PIT value.
common as (

    select
        trade_date,
        series_id,
        vintage_date,
        max_obs  as observation_date,
        value_at_max_obs as value
    from pit_vintage
    where max_obs <= trade_date

),

-- Projection case: the vintage's latest observation is in the future
-- relative to trade_date. We must look inside the vintage for the
-- latest observation <= trade_date. Filter keys hard first so the
-- second scan of stg_macro_series only touches affected vintages.
projection_keys as (

    select distinct trade_date, series_id, vintage_date
    from pit_vintage
    where max_obs > trade_date

),

projection as (

    select
        pk.trade_date,
        pk.series_id,
        pk.vintage_date,
        max(sm.observation_date)               as observation_date,
        max_by(sm.value, sm.observation_date)  as value
    from projection_keys pk
    inner join {{ ref('stg_macro_series') }} sm
        on sm.series_id = pk.series_id
       and sm.vintage_date = pk.vintage_date
       and sm.observation_date <= pk.trade_date
    group by 1, 2, 3

)

select * from common
union all
select * from projection
