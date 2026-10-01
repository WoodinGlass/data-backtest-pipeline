{#
  Singular test: `next_return_positive` is NULL iff `next_log_return`
  is NULL.

  The classification label is derived from `next_log_return`; its NULL
  status must mirror that source column exactly. Any mismatch means a
  bug in the CASE statement in int_returns.sql.
#}

with violations as (

    select
        ticker,
        trade_date,
        case
            when next_return_positive is null and next_log_return is not null
                then 'label NULL but return not NULL'
            when next_return_positive is not null and next_log_return is null
                then 'label not NULL but return is NULL'
        end as reason,
        next_log_return,
        next_return_positive
    from {{ ref('int_returns') }}

)

select * from violations
where reason is not null
