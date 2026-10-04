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


---

## Orchestration (M7)

Three new failure modes once flows are scheduled. All assume the
flows are running in either ephemeral mode (via `dbp-orchestrate run`)
or served mode (via `orchestration.deployments.serve_flows`).

### 1. Flow hangs or times out

**Symptom.** A flow never returns; the Prefect run shows `Running`
past its expected duration.

**Common causes and fixes:**

- **Upstream API slow (yfinance, FRED, SEC).** Each task has a 3-retry
  policy with backoff `[10, 60, 300]`. If the whole flow is stuck on
  one task, check `orchestration/_subprocess.py` logs — the last
  `run:` line names the command.
- **DuckDB lock.** If a previous backtest process is still alive, the
  new flow blocks on the warehouse file. Kill stray processes:
  `pkill -f run_backtest.py`.
- **`serve_flows` blocked on first flow.** In served mode, `serve_flows`
  blocks by design (it is `flow.serve()`). It runs one flow only;
  serve the others in separate processes.

### 2. Webhook alerts not firing

**Symptom.** A flow failed but no Slack message.

**Check:**

- `DBP_ORCH_ALERT_WEBHOOK_URL` is set (env or `.env`).
- The URL responds to a manual `curl -X POST -H 'Content-Type:
  application/json' -d '{"text":"ping"}' $URL`.
- Look for `alert webhook:` in the logs at WARNING level. The poster
  is best-effort and swallows errors so the original failure is never
  masked — check the log to see why the POST failed.

### 3. Cron never fires in served mode

**Symptom.** `serve_flows` is running, but scheduled flows never start.

**Check:**

- Prefect server is reachable. In ephemeral mode there is no server;
  `serve_flows` requires `PREFECT_API_URL` or a local Prefect server
  (M8's docker compose provides one).
- The cron string is UTC. `0 22 * * 1-5` means 22:00 UTC on weekdays,
  not local time. Convert from your local timezone deliberately.
- `flow.serve()` must stay alive. If the process exits, no schedule
  fires. Run under a process manager (systemd, Docker `restart:
  unless-stopped`).


---

## Docker (M8)

### Acceptance test — verify a fresh `make up` works

Run these steps on a machine with a working Docker daemon
(not on Colab). Each step has a clear pass/fail signal.

```bash
# 1. Fresh clone
git clone https://github.com/WoodinGlass/data-backtest-pipeline.git
cd data-backtest-pipeline

# 2. Copy env template (leave FRED_API_KEY empty if you don't have one)
cp .env.example .env

# 3. Build + start
make up                     # ~3 min cold, ~5 s warm

# 4. Health: worker should be running and idle
docker compose ps           # STATUS = Up (healthy)

# 5. Sanity: package importable inside the container
make run CMD='python -c "import ingestion; print(ingestion.__file__)"'
# expect: /app/ingestion/__init__.py

# 6. Sanity: CLI present
make run CMD='python -m orchestration.cli list'   # expect: flow table

# 7. End-to-end: run the full pipeline once (may take 30+ min
#    because of the macro ingest; skip if you only want a smoke test)
make pipeline

# 8. Persistence: stop and restart, state should survive
make down
make up
docker compose exec worker ls -la data/ | head
# expect: warehouse.duckdb, raw/, features/, etc.
```

If any step fails, the failure is almost always one of the three
cases below.

### 1. `make up` fails at build: no space left on device

**Symptom.** `docker compose build` exits with `no space left on
device`.

**Fix.** The image is ~500 MB + build cache. Run:

```bash
docker system prune -a --volumes     # careful: removes all unused images
docker compose build --no-cache
```

If you are on Docker Desktop, increase the disk allocation in
Preferences → Resources → Disk image size.

### 2. `make pipeline` fails at `dbt build`: profile not found

**Symptom.** Container logs show:

```
Could not find profile named 'data_backtest_pipeline'.
```

**Cause.** The image does not contain a `dbt/profiles.yml`, and the
container has no `~/.dbt/profiles.yml` either. Local development
relies on the developer's own profile; the container does not.

**Fix (temporary, from the host).** Create a profile inside the
container and rerun:

```bash
make shell
# inside:
mkdir -p ~/.dbt
cat > ~/.dbt/profiles.yml <<EOF
data_backtest_pipeline:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: /app/data/warehouse.duckdb
EOF
exit
make pipeline
```

**Fix (permanent).** Add `dbt/profiles.yml` to the image (or a
bind mount) and remove the manual step from the acceptance test.
This is a known gap — tracked as future work.

### 3. `make shell` fails: container not running

**Symptom.** `docker compose exec worker bash` returns:

```
service "worker" is not running
```

**Cause.** The worker container exited. Most common reasons:

- The image failed to build (check `docker compose logs worker`).
- Someone ran `make down` and forgot to `make up` again.
- The container crashed during entrypoint bootstrap. Check logs:

```bash
docker compose logs --tail=50 worker
```

**Fix.** `make up` again. If the entrypoint crashed, the logs will
name the failing check (`import ingestion` or a missing mount).
