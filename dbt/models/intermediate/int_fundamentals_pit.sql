{#
  Intermediate: fundamental values on trading days, PIT-correct.

  Split per year via UNION. Each year processes ~250 trade_dates × 31
  tickers × 137 tags ≈ 1.06M pairs. Peak memory ~1/12 of monolithic.
#}

{{ config(
    materialized='table',
    pre_hook=[
        "SET memory_limit='6GB'",
        "SET temp_directory='/tmp/duckdb_spill'",
        "SET preserve_insertion_order=false",
    ]
) }}

{%- set ftags = var('fundamental_tags', []) -%}
{%- set years = [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026] -%}

with tickers as (

    select distinct ticker
    from {{ ref('dim_tickers') }}
    where not is_benchmark

),

curated_tags as (

    select
        split_part(t, ':', 1) as namespace,
        split_part(t, ':', 2) as tag
    from unnest(
        [{% for t in ftags %}'{{ t }}'{{ "," if not loop.last }}{% endfor %}]::varchar[]
    ) as _(t)

),

filtered as (

    select
        f.ticker, f.namespace, f.tag, f.filed, f.period_end, f.value,
        coalesce(datediff('day', f.period_start, f.period_end), 0) as period_days
    from {{ ref('stg_sec_facts') }} f
    inner join curated_tags ct
        on f.namespace = ct.namespace
       and f.tag = ct.tag
    where f.form in ('10-K', '10-Q')
      and f.period_end <= f.filed
      and f.value is not null

),

ranked_within_filing as (

    select
        ticker, namespace, tag, filed, period_end, value,
        row_number() over (
            partition by ticker, namespace, tag, filed
            order by period_end desc, period_days desc
        ) as rn
    from filtered

),

filing_summary as (

    select ticker, namespace, tag, filed, period_end, value
    from ranked_within_filing
    where rn = 1

)

{% for yr in years %}
select
    td.trade_date,
    t.ticker,
    ct.namespace,
    ct.tag,
    arg_max(f.value, f.filed)        as value,
    max(f.filed)                     as filed_used,
    arg_max(f.period_end, f.filed)   as period_end_used
from (
    select distinct trade_date
    from {{ ref('fct_returns_daily') }}
    where extract(year from trade_date) = {{ yr }}
) td
cross join tickers t
cross join curated_tags ct
left join filing_summary f
    on t.ticker = f.ticker
   and ct.namespace = f.namespace
   and ct.tag = f.tag
   and f.filed <= td.trade_date
group by 1, 2, 3, 4
{% if not loop.last %}union all{% endif %}
{% endfor %}
