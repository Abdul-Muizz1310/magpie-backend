#!/usr/bin/env bash
# Docker entrypoint: run DB migrations, apply Procrastinate schema, then exec the CMD.
# Both schema ops are idempotent so restarts are safe.
set -euo pipefail

echo "[entrypoint] applying Alembic migrations"
uv run alembic upgrade head

echo "[entrypoint] applying Procrastinate schema"
uv run procrastinate --app=magpie.queue.app.queue_app schema --apply || {
  echo "[entrypoint] procrastinate schema --apply failed — continuing anyway (may already be applied)"
}

echo "[entrypoint] starting: $*"
exec "$@"
