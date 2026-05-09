# Avatar-Hermes start-example

Mirrors OpenClaw's `start-example/` — three foreground scripts you run
in three terminals to bring up the system locally.

## TL;DR

```bash
# One-time setup — creates ~/.hermes/profiles/{community,agent,nurture-task-worker}
./start-example/setup-profiles.sh

# Terminal 1 — community FastAPI + MCP server (:18790)
./start-example/start-community.sh

# Terminal 2 — device agent (auto-spawns Python service on :8600)
./start-example/start-agent.sh

# Terminal 3 — (optional, debug only) standalone Python device service
./start-example/start-python.sh
```

## What each script does

| Script | Process | Bind | Profile dir |
|---|---|---|---|
| `setup-profiles.sh` | One-shot Hermes profile setup; idempotent | — | creates 3 profiles |
| `start-community.sh` | `python plugins/nurture-community/scripts/serve.py` | `:18790` | `~/.hermes/profiles/community` |
| `start-agent.sh` | `python plugins/nurture/scripts/serve.py` (auto-spawns Python service) | `:8600` (Python service) | `~/.hermes/profiles/agent` |
| `start-python.sh` | Just the Python `server.py` directly | `:8600` | uses `NURTURE_WORKSPACE` env |

## Mapping to OpenClaw

| OpenClaw script | Avatar-Hermes equivalent | Notes |
|---|---|---|
| `start-community.sh` | `start-community.sh` | OpenClaw used `node openclaw.mjs gateway run --port 18790`; we run a Python module that boots FastAPI + MCP under `hermes_plugins.nurture_community`. Same port. |
| `start-agent.sh` | `start-agent.sh` | OpenClaw was the OpenClaw Gateway Node TS process. We run the Python device service + community connector. |
| `start-python.sh` | `start-python.sh` (debug only) | OpenClaw needed this in normal flow because the Python service was a child of the agent gateway via WebSocket node protocol. Avatar-Hermes auto-spawns the Python service from `start-agent.sh` — `start-python.sh` exists only for **debugging without hermes**. |

## Environment variables

| Var | Used by | Default |
|---|---|---|
| `HERMES_HOME` | both | `~/.hermes/profiles/{community,agent}` (scripts auto-set) |
| `NURTURE_COMMUNITY_HOST` | start-community.sh | `127.0.0.1` |
| `NURTURE_COMMUNITY_PORT` | start-community.sh | `18790` |
| `NURTURE_COMMUNITY_DATABASE_URL` | start-community.sh | empty (JSON backend) |
| `NURTURE_NO_COMMUNITY` | start-agent.sh | empty (connector enabled if `community.url` set) |
| `NURTURE_WORKSPACE` | start-python.sh | `~/.hermes/profiles/agent/nurture` |
| `NURTURE_DEVICE_ID` | start-python.sh | empty (auto-pick first online) |
| `NURTURE_DEVICE_PORT` | start-python.sh | `8600` |

## Connecting device → community

By default `start-agent.sh` runs the device offline. To connect it to a
running `start-community.sh`:

```bash
# Edit ~/.hermes/profiles/agent/config.yaml so plugins.nurture.community.url
# points at the community URL. Example for local dev:
hermes --profile agent config set plugins.nurture.community.url \
    "http://127.0.0.1:18790"

# Restart start-agent.sh — on first boot it'll register an avatar with
# the community and immediately start uploading capability/trace/event.
```

## Multi-device

Two devices on one machine:

```bash
# Profile A
HERMES_HOME=$HOME/.hermes/profiles/agent-A ./start-example/start-agent.sh
# Profile B
HERMES_HOME=$HOME/.hermes/profiles/agent-B ./start-example/start-agent.sh
```

Each profile gets its own:
- Python device service port (override via `plugins.nurture.serverPort` in the profile config)
- Community avatar identity (separate JWT token)
- Workspace (recipes, traces, events isolated per agent)

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `start-community.sh` exits with `address already in use` | Another process on `:18790`. Set `NURTURE_COMMUNITY_PORT=18791` and retry. |
| `start-agent.sh` says "Python service did not become healthy" | uiautomator2 / adbutils missing in the venv. Re-run `./setup-hermes.sh` then `pip install -r plugins/nurture/python/requirements.txt`. |
| `/api/devices` returns `[]` | No ADB devices attached. Run `adb devices` outside hermes to confirm. |
| Connector says "401" | Token expired. Delete `~/.hermes/profiles/agent/nurture/agent/community-token.json` and restart — agent will re-register. |
| Worker never picks up tasks | The kanban dispatcher needs the gateway running. Run `hermes gateway start` (separate from `start-agent.sh`) or invoke `hermes kanban dispatch` manually. |
