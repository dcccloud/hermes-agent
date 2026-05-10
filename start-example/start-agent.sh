#!/usr/bin/env bash
# Terminal 2: Avatar-Hermes Device Agent (Python device service + community connector).
#
# Loads the agent profile (~/.hermes/profiles/agent) and runs
# plugins/nurture in the foreground. Auto-spawns the Python FastAPI
# device service on :8600; if community.url is configured, also starts
# the background polling thread.
#
# Equivalent in OpenClaw: start-example/start-agent.sh.

set -euo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$PWD"
VENV_PY="$REPO_ROOT/venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "→ venv not found; run ./setup-hermes.sh first."
    exit 1
fi

# Profile-aware paths.
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes/profiles/agent}"
mkdir -p "$HERMES_HOME"

# ADB platform-tools (for Android automation). Adjust if your toolchain
# lives elsewhere.
if [ -d "$HOME/tools/platform-tools" ]; then
    export PATH="$HOME/tools/platform-tools:$PATH"
fi

echo "Starting nurture device agent"
echo "  HERMES_HOME = $HERMES_HOME"
echo

# --no-community to disable the connector even when a URL is configured
# (useful for offline dev): export NURTURE_NO_COMMUNITY=1
ARGS=()
if [ -n "${NURTURE_NO_COMMUNITY:-}" ]; then
    ARGS+=("--no-community")
fi

# Note the ${ARGS[@]+...} guard — bash 3.x (macOS default) treats an
# empty array as "unset" under `set -u`, which would crash here.
exec "$VENV_PY" plugins/nurture/scripts/serve.py ${ARGS[@]+"${ARGS[@]}"}
