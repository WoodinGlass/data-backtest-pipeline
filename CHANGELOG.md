# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added (M4.5 — Risk framework)

- **ADR 0013** — risk framework contract (staking, entry, limits).
- `risk/config.py` — `RiskSettings` (pydantic-settings, `DBP_RISK_*` env prefix).
- `risk/staking.py` — quarter-Kelly, fixed-fractional, equal-weight, vol-target.
- `risk/entry.py` — threshold, top-N, cross-sectional (benchmark excluded).
- `risk/limits.py` — per-position stop-loss, cooldown, DD derisk, DD halt.
- 104 unit tests in `tests/unit/test_risk_*.py`, including anti-look-ahead
  checks (prefix stability + poison-future).

### Fixed

- **`risk/limits.py`** — row order was sorted by index *label* instead of
  position, corrupting output when input index was non-default. Fixed by
  using `range(len)` as the restore key. Caught by
  `test_row_order_matches_input`.
- **`risk/config.py`** — the cross-field validator rejected `stop_loss_pct`
  deeper than `dd_halt_trigger`. That is a valid configuration (used to
  disable the per-position stop and let DD halt be the only breaker).
  The only hard invariant is `dd_derisk_trigger > dd_halt_trigger`
  (shallower in absolute terms).

### Notes

- Derisk and halt are not independent. Once derisk halves exposure, DD
  grows more slowly, and halt (`-20%`) may never trigger even during a
  severe market move. This is by design. To make halt reachable for
  testing or a stricter regime, set `dd_derisk_factor=1.0`.

