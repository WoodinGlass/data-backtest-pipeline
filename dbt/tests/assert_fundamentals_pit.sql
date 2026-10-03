{#
  Singular test: PIT contract for SEC fundamentals.

  Invariants:
    1. filed_used <= trade_date
    2. period_end_used <= filed_used
    3. period_end_used <= trade_date
#}

select
    'filed_used > trade_date' as reason,
    trade_date, ticker, namespace, tag, filed_used, period_end_used
from {{ ref('int_fundamentals_pit') }}
where filed_used is not null
  and filed_used > trade_date

union all

select
    'period_end_used > filed_used' as reason,
    trade_date, ticker, namespace, tag, filed_used, period_end_used
from {{ ref('int_fundamentals_pit') }}
where period_end_used is not null
  and period_end_used > filed_used

union all

select
    'period_end_used > trade_date' as reason,
    trade_date, ticker, namespace, tag, filed_used, period_end_used
from {{ ref('int_fundamentals_pit') }}
where period_end_used is not null
  and period_end_used > trade_date
