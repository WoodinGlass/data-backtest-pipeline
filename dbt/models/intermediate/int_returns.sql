{#
  Intermediate: log returns and forward returns, point-in-time.

  ── Anti-leakage contract ──────────────────────────────────
  For each row (ticker, trade_date = t):

    log_return       : ln(adj_close_t / adj_close_{t-1})
                       Known at end of t. SAFE as a feature for t+1.
                       NULL on the first bar per ticker.

    next_trade_date  : the next available trade date per ticker.
    next_adj_close   : adj_close at t+1.
    next_log_return  : ln(adj_close_{t+1} / adj_close_t)
                       Known at end of t+1. THIS IS THE LABEL.
                       NULL on the last bar per ticker.
    next_return_positive : (next_log_return > 0)
                       Binary label. NULL iff next_log_return is NULL.

  Any feature computed from this table for prediction at t+1 MUST
  come from columns known at t (log_return and leftward), never from
  next_* columns.

  ── Why log(adj_close) and not log(close) ──────────────────
  Adjusted close accounts for splits and dividends, so the log
  return is the actual economic return the holder experiences.
  Vendor noise (~3e-5, see ADR 0006) is orders of magnitude below
  any signal we are chasing.
#}

with prices as (

    select * from {{ ref('stg_prices') }}

),

windowed as (

    select
        ticker,
        trade_date,
        close,
        adj_close,
        volume,

        -- Point-in-time: known at end of day t.
        log(adj_close / lag(adj_close) over w) as log_return,

        -- Forward references: NULL at the tail per ticker.
        lead(trade_date)  over w as next_trade_date,
        lead(close)       over w as next_close,
        lead(adj_close)   over w as next_adj_close,
        log(lead(adj_close) over w / adj_close) as next_log_return

    from prices
    window w as (partition by ticker order by trade_date)

)

select
    *,
    case
        when next_log_return is null then null
        when next_log_return > 0      then true
        else                               false
    end as next_return_positive
from windowed
