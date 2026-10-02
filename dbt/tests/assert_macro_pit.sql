{#
  Singular test: the PIT contract for macro must hold.

  Invariants:
    1. vintage_date <= trade_date
       (we never look at a vintage published after the trade date)
    2. observation_date <= trade_date
       (we never look at a period that had not happened by trade_date)

  Violations indicate look-ahead bias, the exact failure mode this
  project exists to prevent. Hard fail with detail so the source of
  the bug is identifiable.
#}

select
    trade_date,
    series_id,
    vintage_date,
    observation_date,
    'vintage_date > trade_date' as reason
from {{ ref('int_macro_daily') }}
where vintage_date > trade_date

union all

select
    trade_date,
    series_id,
    vintage_date,
    observation_date,
    'observation_date > trade_date' as reason
from {{ ref('int_macro_daily') }}
where observation_date > trade_date
