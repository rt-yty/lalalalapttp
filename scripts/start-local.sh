#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/opt/homebrew/opt/postgresql@16/bin:/opt/homebrew/opt/postgresql@17/bin:$PATH"

for travel_tool in uv npm; do
  command -v "$travel_tool" >/dev/null || { echo "Для запуска нужен $travel_tool."; exit 1; }
done
if [ ! -d frontend/node_modules ]; then
  npm --prefix frontend ci
fi
npm --prefix frontend run build
uv sync --locked

# Use an isolated local database unless an existing deployment URL was supplied.
if [ -z "${DATABASE_URL:-}" ]; then
  for travel_tool in initdb pg_ctl createdb psql; do
    command -v "$travel_tool" >/dev/null || { echo "Установите PostgreSQL или задайте DATABASE_URL."; exit 1; }
  done
  TRAVEL_PG_PORT="${TRAVEL_PG_PORT:-55432}"
  TRAVEL_CLUSTER="$PWD/data/local-postgres"
  mkdir -p "$TRAVEL_CLUSTER/socket"
  if [ ! -f "$TRAVEL_CLUSTER/cluster/PG_VERSION" ]; then
    initdb -D "$TRAVEL_CLUSTER/cluster" -U travel --auth=trust --encoding=UTF8 --locale=C > "$TRAVEL_CLUSTER/init.log"
  fi
  if ! pg_ctl -D "$TRAVEL_CLUSTER/cluster" status >/dev/null 2>&1; then
    pg_ctl -D "$TRAVEL_CLUSTER/cluster" -l "$TRAVEL_CLUSTER/server.log" -o "-h 127.0.0.1 -p $TRAVEL_PG_PORT" -w start
  fi
  if [ "$(psql -h 127.0.0.1 -p "$TRAVEL_PG_PORT" -U travel -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname='travel'")" != "1" ]; then
    createdb -h 127.0.0.1 -p "$TRAVEL_PG_PORT" -U travel travel
  fi
  export DATABASE_URL="postgresql://travel@127.0.0.1:$TRAVEL_PG_PORT/travel"
fi

export AGENT_MODEL_BACKEND="${AGENT_MODEL_BACKEND:-codex}"
export AGENT_DATA_MODE="${AGENT_DATA_MODE:-live}"
export AGENT_CHECKOUT_MODE="${AGENT_CHECKOUT_MODE:-disabled}"
export FRONTEND_DIST="$PWD/frontend/dist"
TRAVEL_PORT="${TRAVEL_PORT:-8780}"
echo "О!дин запрос — http://127.0.0.1:$TRAVEL_PORT"
exec .venv/bin/uvicorn travel_agent.asgi:app --host 127.0.0.1 --port "$TRAVEL_PORT" --workers 1 --no-access-log
