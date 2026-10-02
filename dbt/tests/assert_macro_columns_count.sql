{#
  Singular test: fct_macro_daily has exactly
  (n_macro_series + 1) columns.

  Approach: compare the row count of `describe select * from model`
  against the expected count from the var. No run_query, no Jinja
  runtime magic — just a plain SQL singular test that returns a row
  when the count is wrong, and zero rows otherwise.
#}

{%- set expected_n = var('macro_series_ids', []) | length + 1 -%}

with cols as (

    select count(*) as n
    from (describe select * from {{ ref('fct_macro_daily') }})

)

select n, {{ expected_n }} as expected_n
from cols
where n != {{ expected_n }}
