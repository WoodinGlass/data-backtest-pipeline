# ADR 0017: Docker and One-Command Reproducibility

- **Status:** Accepted
- **Date:** 2026-10-05
- **Related:** ADR 0002 (DuckDB), ADR 0015 (MLflow), ADR 0016 (Prefect)

## Context

By M7 the project has a full pipeline that runs end-to-end from a
terminal: `make setup-dev`, `make ingest`, `make dbt-build`,
`make quality`, `make features`, `make backtest`. Every step is
idempotent and every dependency is declared in `pyproject.toml` with
self-contained extras.

What is missing is **a single command that reproduces the whole
pipeline on a fresh machine**. The current setup assumes the user
has Python 3.11, the right system libraries (DuckDB compiles fine
everywhere, but git and make are assumed), and knows the sequence of
`make` targets. That is fine for a developer machine; it is not
reproducible for a reviewer who just wants to run the pipeline once.

M8 adds a Docker layer whose sole purpose is to make `make up` work
from a clean checkout. It must **not** change pipeline semantics,
must be optional, and must not silently consume a lot of disk or RAM.

## Decision

We adopt Docker with twelve decisions. All are implemented via a
single `Dockerfile`, a `.dockerignore`, a `docker-compose.yml`, and
new `make` targets.

### 1. Base image: `python:3.11-slim`

- `python:3.11` matches `requires-python = ">=3.11"` in
  `pyproject.toml`. CI runs 3.11, so the container mirrors CI.
- `-slim` (Debian bookworm) is ~120 MB versus ~900 MB for the full
  image. It includes everything the pipeline needs: `gcc` is not
  required because all dependencies ship wheels for manylinux.
- No Alpine. Alpine's musl libc breaks some scientific wheels
  (numpy, pandas). The size saving is not worth the pain.

### 2. Single-stage build

- The project is small (a dozen Python packages, no compiled C
  extensions built from source). Multi-stage builds would shave
  tens of MB at the cost of a more complex Dockerfile.
- If the image ever grows past ~800 MB, a multi-stage build is
  worth revisiting. Today it does not.

### 3. Non-root user

- Container runs as `app` (UID 1000, GID 1000) by default. Root is
  never used at runtime.
- Build-time `pip install` runs as root, which is fine because the
  layer is thrown away if the image is rebuilt.
- Rationale: defense in depth. Nothing in the pipeline needs root.

### 4. Cache-friendly dependency install

- The Dockerfile copies `pyproject.toml` and `README.md` first,
  then runs `pip install --no-deps -e .` to install the *package
  metadata and pinned deps*, then copies the rest of the source.
- This way, editing a Python file does not invalidate the
  dependency layer. A full rebuild after a source-only change
  takes ~5 seconds.
- Rationale: standard pattern; developer iteration speed.

### 5. Volume strategy: bind mounts for state

The container writes to three directories that must persist across
`docker compose down` / `up`:

| Host path | Container path | Contents |
|---|---|---|
| `./data` | `/app/data` | Raw layer, DuckDB warehouse, features, backtest runs |
| `./mlruns` | `/app/mlruns` | MLflow SQLite + artifacts (M6) |
| `./reports` | `/app/reports` | Quality gate JSON reports |

Everything else (source code, `pyproject.toml`, `dbt/`) is baked
into the image at build time. Source changes require a rebuild;
state changes do not.

`.env` is bind-mounted read-only at `/app/.env` if present. No
secrets ever go into the image.

### 6. Services: `worker` (required) + `prefect-server` (optional)

Two services, one profile each.

- **`worker`** (default profile, always started): the pipeline
  executor. Runs `sleep infinity` by default so the container stays
  up for `docker compose exec`. All Make targets that call into the
  pipeline go through `docker compose exec worker`.
- **`prefect-server`** (profile `served`): runs a Prefect 3 server
  with a SQLite backend, used only when the user wants M7's served
  mode. Not started by default.

Rationale: most users only need `worker`. Starting a Prefect server
by default would add ~200 MB of RAM and a second port for no benefit.

### 7. Default command: `sleep infinity`

The `worker` container stays alive after `make up` so the user can
`docker compose exec worker bash` and poke at the state. This is
deliberately different from a "run-once and exit" container: the
project is exploratory, and being able to shell in matters.

For one-shot execution, `make pipeline` runs the full pipeline in a
one-off container (`docker compose run --rm worker <cmd>`).

### 8. Entrypoint: optional bootstrap

`scripts/docker_entrypoint.sh`:

- If `DBP_DOCKER_BOOTSTRAP=1`, runs a minimal setup (create
  `data/`, `mlruns/`, `reports/`; verify `.env`; verify the package
  is importable) before `exec`-ing the command.
- Otherwise, just `exec`s the given command.
- Always ends with `exec "$@"` so signals propagate and PID 1 is the
  user's command, not the shell.

Rationale: keeps the entrypoint trivial and predictable. Bootstrap
logic is opt-in so repeated `docker compose exec` calls are fast.

### 9. Compose profiles for served mode

```yaml
services:
  worker:
    profiles: []              # always
  prefect-server:
    profiles: ["served"]      # opt-in
```

Activate with `docker compose --profile served up`.

Rationale: profiles are the standard Compose way to make optional
services first-class. No env-var gymnastics.

### 10. No network exposure by default

Only `prefect-server` (when active) publishes a port
(`4200:4200`). The `worker` publishes nothing. All inter-service
communication happens on the default Compose bridge network.

Rationale: a single-developer pipeline does not need to be reachable
from the host except for the Prefect UI.

### 11. Make targets wrap compose

New targets:

| Target | Command | Purpose |
|---|---|---|
| `make up` | `docker compose up -d --build` | Build + start worker |
| `make down` | `docker compose down` | Stop and remove |
| `make logs` | `docker compose logs -f worker` | Tail logs |
| `make shell` | `docker compose exec worker bash` | Interactive shell |
| `make run CMD=...` | `docker compose exec worker sh -lc "$(CMD)"` | Exec one command |
| `make pipeline` | `docker compose run --rm worker sh -lc "..."` | Full pipeline, one shot |
| `make docker-build` | `docker compose build` | Build only |
| `make prefect-up` | `docker compose --profile served up -d` | Served mode |

Rationale: the Makefile is already the project's entry point. Docker
targets live alongside the existing ones so `make help` shows one
coherent list.

### 12. Verification strategy

Docker is not available in Colab (no daemon). M8 verification is
therefore **static**:

- `docker-compose.yml` parses via `yaml.safe_load`.
- `Dockerfile` parses via the `dockerfile-parse` library.
- `scripts/docker_entrypoint.sh` passes `bash -n`.
- New Make targets exist and dry-run without error (`make -n`).

**End-to-end verification (actual `make up` + pipeline run) is
documented in `docs/runbook.md` and left for the user's local
environment.** The ADR is explicit about this: M8 ships verified
files, not a verified image, because the build environment is not
available here.

## Consequences

### Positive

- `git clone && cp .env.example .env && make up` is a two-command
  reproducibility story.
- Container state (raw layer, warehouse, MLflow runs) persists on
  the host and survives `docker compose down`.
- Non-root, no published ports, no secrets in the image.
- Pipeline semantics unchanged: every flow still calls the same
  CLI, and `make pipeline` inside the container produces the same
  outputs as `make pipeline` on the host.

### Negative

- Docker adds ~400 MB to a fresh machine (image + layers).
- Bind mounts on macOS and Windows have slower IO than native.
  Acceptable for daily-bar workloads, painful for tick data.
- Layer caching is not perfect: any change to `pyproject.toml`
  invalidates the dependency layer. This is a design trade-off
  (correctness over speed).
- Colab cannot verify the build. There is a small risk that the
  first local `make up` reveals a typo that static checks missed.

### Neutral

- Docker Swarm, Kubernetes, and multi-host orchestration are out
  of scope. A future ADR would be needed for any of those.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| No Docker | Reviewer must install Python, make, and git by hand; not reproducible |
| Conda environment file | Heavier than pip for this dependency set; adds a second package manager |
| Devcontainer / VS Code remote | Editor-specific; still needs a Dockerfile underneath |
| Nix | Reproducible, but a steep learning curve for reviewers |
| Podman-only | Compatible with Docker CLI but not universally installed |
| Multi-stage build | Complexity not justified at this image size |
| Run-once container (exit after pipeline) | Users cannot `exec` in to inspect state; exploratory workflow suffers |

## References

- Docker docs: *Build best practices*.
- Compose docs: *Profiles*.
- `python:3.11-slim` image digest documented in the Dockerfile
  comment for reproducibility.
