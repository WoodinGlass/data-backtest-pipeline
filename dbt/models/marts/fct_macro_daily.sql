{#
  Marts: macro values on trading days, one column per series.

  Input:  int_macro_daily (long format: trade_date, series_id, value)
  Output: one row per trade_date, one column per series, prefixed
          `macro_` and lowercased (e.g. FEDFUNDS -> macro_fedfunds).

  Wide format chosen because downstream feature builders (M4) want
  specific series by name, and per-column tests are easier to write
  and document than per-row filters.

  The column list comes from the `macro_series_ids` var (generated from
  config/macro_series.yml). Adding a series to the registry requires
  regenerating the var, which `scripts/sync_macro_var.py` automates.

  Grain: one row per trade_date.
#}

{{ config(materialized='table') }}

{%- set series_ids = var('macro_series_ids', []) -%}

select
    trade_date,

    {%- for sid in series_ids %}
    max(case when series_id = '{{ sid }}' then value end)
        as macro_{{ sid | lower }}{{ "," if not loop.last }}
    {%- endfor %}

from {{ ref('int_macro_daily') }}
group by trade_date
