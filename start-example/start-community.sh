#!/usr/bin/env bash
# Terminal 1: Avatar-Hermes Community FastAPI + MCP server (:18790)
#
# Loads the community profile (~/.hermes/profiles/community) and runs
# plugins/nurture-community in the foreground. Bind defaults to
# loopback — set HOST / PORT to override.
#
# Equivalent in OpenClaw: start-example/start-community.sh.

set -euo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$PWD"
VENV_PY="$REPO_ROOT/venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "→ venv not found; run ./setup-hermes.sh first."
    exit 1
fi

# Profile-aware paths so this Hermes process doesn't interfere with the
# user's main hermes profile.
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/profiles/community}"
mkdir -p "$HERMES_HOME"

# Optional: use PostgreSQL by exporting NURTURE_COMMUNITY_DATABASE_URL.
# When empty, falls back to JSON file backend under
#   $HERMES_HOME/nurture-community/knowledge/.
HOST="${NURTURE_COMMUNITY_HOST:-127.0.0.1}"
PORT="${NURTURE_COMMUNITY_PORT:-18790}"

echo "Starting nurture-community"
echo "  HERMES_HOME = $HERMES_HOME"
echo "  bind        = $HOST:$PORT"
echo "  backend     = ${NURTURE_COMMUNITY_DATABASE_URL:+PostgreSQL}${NURTURE_COMMUNITY_DATABASE_URL:-JSON files}"
echo

exec "$VENV_PY" plugins/nurture-community/scripts/serve.py \
    --host "$HOST" \
    --port "$PORT"
