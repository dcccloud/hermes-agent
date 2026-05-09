#!/usr/bin/env bash
# One-shot setup: create the Hermes profiles for community + agent + worker.
#
# Run once per machine. Idempotent — re-running tweaks settings without
# clobbering existing data.
#
# What it does:
#   1. Ensures the venv exists (running ./setup-hermes.sh if missing)
#   2. Creates ~/.hermes/profiles/{community,agent,nurture-task-worker}/
#   3. Writes their config.yaml with the right plugins enabled
#   4. Prints next-step commands the operator should run

set -euo pipefail
cd "$(dirname "$0")/.."

REPO_ROOT="$PWD"
VENV_PY="$REPO_ROOT/venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "→ venv not found; running ./setup-hermes.sh"
    ./setup-hermes.sh
fi

HERMES_BIN="$REPO_ROOT/venv/bin/hermes"

# Profile dirs live under ~/.hermes/profiles/<name>/
PROFILES_ROOT="${HERMES_HOME:-$HOME/.hermes}/profiles"
mkdir -p "$PROFILES_ROOT"

# ----------------------------------------------------------------------
# Community profile
# ----------------------------------------------------------------------
COMMUNITY_DIR="$PROFILES_ROOT/community"
mkdir -p "$COMMUNITY_DIR"
if [ ! -f "$COMMUNITY_DIR/config.yaml" ]; then
cat > "$COMMUNITY_DIR/config.yaml" <<'EOF'
# Avatar-Hermes community profile.
# Hosts the community FastAPI server + MCP endpoint.

plugins:
  enabled:
    - nurture-community
  nurture-community:
    enabled: true
    host: 127.0.0.1
    port: 18790
    # databaseUrl: "postgresql://user:pass@host/db?sslmode=require"
    # Leave databaseUrl empty for JSON file backend (single-machine dev).

# Optional: configure recipe fusion to use a cheap independent model.
# auxiliary:
#   recipe_fusion:
#     provider: openrouter
#     model: anthropic/claude-haiku
EOF
echo "✓ created $COMMUNITY_DIR/config.yaml"
else
    echo "  community profile already exists at $COMMUNITY_DIR (skipping)"
fi

# ----------------------------------------------------------------------
# Agent (device) profile
# ----------------------------------------------------------------------
AGENT_DIR="$PROFILES_ROOT/agent"
mkdir -p "$AGENT_DIR"
if [ ! -f "$AGENT_DIR/config.yaml" ]; then
cat > "$AGENT_DIR/config.yaml" <<'EOF'
# Avatar-Hermes device-agent profile.
# Manages the Python device service + community connector.

plugins:
  enabled:
    - nurture
  nurture:
    enabled: true
    pythonPath: python3
    serverHost: 127.0.0.1
    serverPort: 8600
    autoStart: true
    appEnv: dev
    # deviceId: "" — leave blank to auto-pick the first online ADB device.
    # Set explicitly if you have multiple devices attached.
    # deviceId: "3034337076001MH"

    # Optional community connector. Empty url = run offline.
    community:
      url: ""
      pollIntervalCapsMs: 60000
      pollIntervalTaskMs: 30000
      workerProfile: nurture-task-worker
EOF
echo "✓ created $AGENT_DIR/config.yaml"
else
    echo "  agent profile already exists at $AGENT_DIR (skipping)"
fi

# ----------------------------------------------------------------------
# Worker profile (for kanban-spawned community task execution)
# ----------------------------------------------------------------------
WORKER_DIR="$PROFILES_ROOT/nurture-task-worker"
mkdir -p "$WORKER_DIR"
if [ ! -f "$WORKER_DIR/config.yaml" ]; then
cat > "$WORKER_DIR/config.yaml" <<'EOF'
# Worker profile spawned by the kanban dispatcher when a community
# task arrives. Runs short-lived: load nurture, execute the task,
# call nurture_task_report, exit.

plugins:
  enabled:
    - nurture
  nurture:
    enabled: true
    autoStart: false   # parent already runs the Python service

delegation:
  subagent_auto_approve: true
EOF
echo "✓ created $WORKER_DIR/config.yaml"
else
    echo "  worker profile already exists at $WORKER_DIR (skipping)"
fi

# ----------------------------------------------------------------------
# Print next steps
# ----------------------------------------------------------------------
echo
echo "Profiles ready:"
echo "  $COMMUNITY_DIR"
echo "  $AGENT_DIR"
echo "  $WORKER_DIR"
echo
echo "Next steps:"
echo "  1. (optional) Edit $AGENT_DIR/config.yaml to point at a community URL"
echo "     and a specific deviceId."
echo "  2. (optional) Add provider API keys to ~/.hermes/.env if you need"
echo "     VLM fallback / recipe fusion."
echo "  3. Start the community side in terminal 1:"
echo "       ./start-example/start-community.sh"
echo "  4. Start the device agent side in terminal 2:"
echo "       ./start-example/start-agent.sh"
echo "  5. (optional, debug only) Run the Python service standalone in"
echo "     a third terminal — useful when iterating on operations:"
echo "       ./start-example/start-python.sh"
