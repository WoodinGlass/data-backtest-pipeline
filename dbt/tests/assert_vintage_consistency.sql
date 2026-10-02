{#
  Singular test: every vintage of a series must be internally
  consistent, i.e. no duplicated observation_date within the same
  (series_id, vintage_date).

  Note on the previous assertion
  ------------------------------
  An earlier version of this test asserted
  `observation_date <= vintage_date`. That is wrong for projection
  series (GDPPOT, IORB, IOER): FRED legitimately stores projected
  future values under a single vintage. The correct PIT invariant is
  enforced one layer up, in int_macro_daily, where we only join
  observation_date <= trade_date. See ADR 0009.

  This test catches a real ingestion bug class: if the vintage
  reconstruction in client.py ever produces duplicated
  observation_dates within one vintage, we want to know immediately.
#}

select
    series_id,
    vintage_date,
    observation_date,
    count(*) as n
from {{ ref('stg_macro_series') }}
group by 1, 2, 3
having count(*) > 1
