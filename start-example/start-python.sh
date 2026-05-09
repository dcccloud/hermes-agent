#!/usr/bin/env bash
# Terminal 3 (optional, debug-only): Standalone Python device service.
#
# Use this when you're iterating on operation handlers / recipe
# generation / VLM tuning and want a Python service WITHOUT a parent
# hermes process spawning it.
#
# In normal flow, start-agent.sh already auto-spawns this — you do
# NOT need to run it separately.
#
# Equivalent in OpenClaw: start-example/start-python.sh.

set -euo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$PWD"
VENV_PY="$REPO_ROOT/venv/bin/python"
PYTHON_DIR="$REPO_ROOT/plugins/nurture/python"

if [ ! -x "$VENV_PY" ]; then
    echo "→ venv not found; run ./setup-hermes.sh first."
    exit 1
fi

if [ ! -f "$PYTHON_DIR/server.py" ]; then
    echo "→ Python device service not found at $PYTHON_DIR — Phase 1 incomplete?"
    exit 1
fi

# ADB platform-tools.
if [ -d "$HOME/tools/platform-tools" ]; then
    export PATH="$HOME/tools/platform-tools:$PATH"
fi

# NURTURE_WORKSPACE controls where data/recipes/, data/traces/, etc. live.
# When started by hermes plugins/nurture, this is set to
# $HERMES_HOME/nurture/. Pick the same default here so dev runs share data.
export NURTURE_WORKSPACE="${NURTURE_WORKSPACE:-$HOME/.hermes/profiles/agent/nurture}"
mkdir -p "$NURTURE_WORKSPACE"

# Optional: bind a specific device. Defaults to "" (auto-pick first online).
DEVICE_ID="${NURTURE_DEVICE_ID:-}"

# Optional: alternate port if 8600 is taken.
PORT="${NURTURE_DEVICE_PORT:-8600}"

echo "Starting Python device service (debug)"
echo "  workspace = $NURTURE_WORKSPACE"
echo "  port      = $PORT"
echo "  device-id = ${DEVICE_ID:-(auto)}"
echo

ARGS=(--host 127.0.0.1 --port "$PORT" --workspace "$NURTURE_WORKSPACE")
if [ -n "$DEVICE_ID" ]; then
    ARGS+=(--device-id "$DEVICE_ID")
fi

cd "$PYTHON_DIR"
exec "$VENV_PY" server.py "${ARGS[@]}"
