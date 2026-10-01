{#
  Singular test: log returns must lie in (-1, 1).

  Daily equity log returns have |r| < 1 by construction for any
  non-trivial market (a 172% move in a day would be required to hit
  1; a -100% move to hit -1). Values outside this range almost
  certainly indicate a data error (split mishandled, adj_close
  glitch) rather than a real move.

  NULLs are allowed at the per-ticker boundaries — those are the
  first and last bar respectively — and are not flagged here.
#}

with violations as (

    select
        ticker,
        trade_date,
        'log_return out of (-1, 1)' as reason,
        log_return as value
    from {{ ref('int_returns') }}
    where log_return is not null
      and (log_return <= -1 or log_return >= 1)

    union all

    select
        ticker,
        trade_date,
        'next_log_return out of (-1, 1)' as reason,
        next_log_return as value
    from {{ ref('int_returns') }}
    where next_log_return is not null
      and (next_log_return <= -1 or next_log_return >= 1)

)

select * from violations
