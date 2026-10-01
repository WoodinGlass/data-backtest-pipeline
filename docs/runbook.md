# Runbook

Operational playbook for the three most common failures.

## 1. Ingestion fails (yfinance returns empty or partial)

**Symptom:** Prefect flow `daily_ingest` fails, or fewer tickers returned than expected.
**Impact:** Missing bars for today. Last good snapshot remains usable.
**Actions:**
1. Check yfinance status / Yahoo Finance uptime.
2. Inspect structured logs: filter `event="fetch_failed"`.
3. Re-run the flow — writes are idempotent, safe to retry.
4. If a specific ticker is persistently missing (delisted), acknowledge the
   freshness warning; do **not** patch raw.

## 2. dbt build fails on a test

**Symptom:** CI or scheduled flow red on `not_null` / `unique` / `relationships`.
**Impact:** Downstream features and backtest are stale or wrong.
**Actions:**
1. Identify the failing test and the offending model.
2. Query the raw layer for offending rows (they are immutable — never delete).
3. Decide: source bug (fix ingestion) or model bug (fix dbt).
4. Add a regression test before re-running.

## 3. Drift alarm fires

**Symptom:** `monitoring` reports feature distribution shift above threshold.
**Impact:** Model may be miscalibrated. Predictions still produced, flagged.
**Actions:**
1. Confirm drift is real (not a schema change).
2. Compare calibration on the latest window vs training window.
3. If confirmed, open a retrain ticket; do not silently adjust thresholds.

---

_This runbook will be expanded as M7-M10 land._
