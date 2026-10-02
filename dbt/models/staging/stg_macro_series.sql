{#
  Staging: raw macro -> typed, renamed, one row per
  (series_id, observation_date, vintage_date).

  ── PIT-effective vintage_date ──────────────────────────────
  For "latest-mode" series (see macro_latest_mode_ids var and
  config/macro_series_latest_only.yml), FRED's vintage_date is the
  date of the most recent refresh (e.g. 2026-09-30 for all rows),
  not the actual first-publication date. Using it directly would
  make these series invisible to any backtest before that date.

  We therefore set `vintage_date := observation_date` for latest-mode
  series. Rationale: these series are never revised; the value for
  observation_date is published same day (DGS10, VIX) or next
  business day at most. Using the observation date as its PIT-effective
  vintage is the correct, conservative choice for daily equity
  prediction (we predict t+1 using data known at end of t).

  For full-mode series, we keep FRED's vintage_date unchanged.

  See ADR 0009.
#}

{%- set latest_mode_ids = var('macro_latest_mode_ids', []) -%}

with source as (

    select * from {{ source('raw', 'macro') }}

),

adjusted as (

    select
        cast(series_id        as varchar) as series_id,
        cast(observation_date as date)    as observation_date,

        {%- if latest_mode_ids | length > 0 %}
        case
            when series_id in ({{ "'" + latest_mode_ids | join("','") + "'" }})
            then cast(observation_date as date)  -- PIT-effective
            else cast(vintage_date as date)
        end as vintage_date,
        {%- else %}
        cast(vintage_date as date) as vintage_date,
        {%- endif %}

        cast(value            as double)  as value
    from source

)

select * from adjusted
