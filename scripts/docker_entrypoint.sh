#!/usr/bin/env bash
# ---------------------------------------------------------------------
# Container entrypoint for data-backtest-pipeline.
# See docs/adr/0017-docker.md sections 7-8.
#
# Behaviour:
#   1. If DBP_DOCKER_BOOTSTRAP=1, run a minimal bootstrap once.
#   2. exec the user command so signals propagate and PID 1 is the
#      user command (not this shell).
#
# The script must remain POSIX-ish and idempotent. It is safe to run
# on every `docker compose exec` because bootstrap is guarded.
# ---------------------------------------------------------------------
set -eu

log() {
    printf '[entrypoint] %s\n' "$*" >&2
}

bootstrap() {
    log "bootstrapping (DBP_DOCKER_BOOTSTRAP=1)"

    # 1. Required runtime directories (bind-mounted at compose level,
    #    but still created here for standalone `docker run`).
    mkdir -p /app/data /app/mlruns /app/reports

    # 2. Warn loudly if .env is missing. We do NOT copy .env.example
    #    automatically: forcing an explicit choice avoids surprising
    #    a user with placeholder credentials.
    if [ ! -f /app/.env ]; then
        log "WARN: /app/.env not found."
        log "      Copy .env.example to .env on the host, then rerun."
    fi

    # 3. Sanity check: the package is importable in editable mode.
    if ! python -c 'import ingestion' >/dev/null 2>&1; then
        log "ERROR: package not importable. Did the image build succeed?"
        exit 1
    fi

    log "bootstrap ok"
}

# Only run bootstrap for commands that actually need the environment.
# `sleep`, `bash`, `sh`, and `tini --version` are exempt.
case "${1:-}" in
    sleep|bash|sh|tini)
        ;;
    "")
        ;;
    *)
        if [ "${DBP_DOCKER_BOOTSTRAP:-0}" = "1" ]; then
            bootstrap
        fi
        ;;
esac

log "exec: $*"
exec "$@"
