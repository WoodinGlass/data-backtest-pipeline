{#
  Custom freshness check for the external Parquet source.

  dbt's built-in source freshness relies on a `loaded_at_field` query
  against the source. Because our source is a glob of Parquet files,
  the natural freshness signal is the *maximum trade_date* across all
  files, compared to the current date.

  This macro returns a single row: (max_date, days_since, status).
  Called from models or directly in a test.
#}

{% macro raw_prices_freshness() %}
    with bounds as (
        select max(cast(date as date)) as max_date
        from read_parquet('{{ var("raw_prices_glob") }}', union_by_name = true)
    )
    select
        max_date,
        date_diff('day', max_date, current_date) as days_since,
        case
            when date_diff('day', max_date, current_date) > {{ var("freshness_error_days") }}
                then 'error'
            when date_diff('day', max_date, current_date) > {{ var("freshness_warn_days") }}
                then 'warn'
            else 'pass'
        end as status
    from bounds
{% endmacro %}
