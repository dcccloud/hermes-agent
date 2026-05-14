# Avatar-Hermes start-example

Mirrors OpenClaw's `start-example/` — shell scripts to bring up the system
locally + the end-to-end flow for publishing tasks and watching them
execute on a real device.

---

## TL;DR (5 minutes)

```bash
# 0. One-time setup
./start-example/setup-profiles.sh

# 1. Terminal A — community FastAPI + MCP
./start-example/start-community.sh

# 2. Edit ~/.hermes/profiles/agent/config.yaml — point community.url at
#    http://127.0.0.1:18790 and set deviceId from `adb devices`.

# 3. Terminal B — device agent
./start-example/start-agent.sh

# 4. Terminal C — publish a task
./venv/bin/python plugins/nurture-community/scripts/tasks.py create \
    --app douyin \
    --description "在抖音首页点赞 1 个美食视频"

# Wait <30s. Agent polls, dispatches to Kanban. To actually execute,
# the Kanban dispatcher needs `hermes` on PATH AND an LLM provider
# configured in the worker profile (see "LLM setup" below).
export PATH="$PWD/venv/bin:$PATH"
HERMES_HOME=$HOME/.hermes/profiles/agent hermes kanban dispatch
```

---

## What each script does

| Script | Process | Bind | Profile dir |
|---|---|---|---|
| `setup-profiles.sh` | One-shot Hermes profile setup; idempotent | — | creates 3 profiles |
| `start-community.sh` | `python plugins/nurture-community/scripts/serve.py` | `:18790` | `~/.hermes/profiles/community` |
| `start-agent.sh` | `python plugins/nurture/scripts/serve.py` (auto-spawns Python service) | `:8600` (Python service) | `~/.hermes/profiles/agent` |
| `start-python.sh` | Just the Python `server.py` directly | `:8600` | uses `NURTURE_WORKSPACE` env |

---

## End-to-end flow

```
┌──────────────────────────────────────────────────────────────────┐
│  Terminal A  ./start-example/start-community.sh                  │
│  - FastAPI :18790                                                │
│  - MCP server /mcp                                               │
└──────────────────┬───────────────────────────────────────────────┘
                   │ REST + MCP (Bearer JWT)
┌──────────────────┴───────────────────────────────────────────────┐
│  Terminal B  ./start-example/start-agent.sh                      │
│  - Python device service :8600  ← talks to your Android over ADB │
│  - Community connector thread   ← 60s upload / 30s poll          │
│  - On first start: POST /api/avatar/register → save JWT          │
│                                                                  │
│  Terminal C  publish a task                                      │
│  └─→ tasks.py create writes task-dispatch.json on community side │
│        ↓                                                         │
│  Terminal B's connector polls (30s), notices new task            │
│        ↓                                                         │
│  kanban_bridge.maybe_dispatch_new_tasks → kanban_db.create_task  │
│        ↓                                                         │
│  Kanban card status=ready, assignee=nurture-task-worker          │
│        ↓                                                         │
│  Terminal D  hermes kanban dispatch                              │
│  └─→ Spawns a worker hermes process for `nurture-task-worker`    │
│        ↓                                                         │
│  Worker reads task body, calls nurture_execute via HTTP :8600    │
│        ↓                                                         │
│  Python device service drives ADB → operation completes          │
│        ↓                                                         │
│  Worker calls nurture_task_report → POST /api/task/complete      │
│        ↓                                                         │
│  Community marks task done; kanban card → done                   │
└──────────────────────────────────────────────────────────────────┘
```

---

## Publishing tasks (community side)

```bash
# Create
./venv/bin/python plugins/nurture-community/scripts/tasks.py create \
    --app douyin \
    --description "在抖音首页点赞 1 个美食视频" \
    --priority normal

# Optionally constrain who can pick it up
./venv/bin/python plugins/nurture-community/scripts/tasks.py create \
    --app douyin \
    --description "..." \
    --platform android \
    --require-cap douyin.give_a_like

# List
./venv/bin/python plugins/nurture-community/scripts/tasks.py list

# Show one
./venv/bin/python plugins/nurture-community/scripts/tasks.py show task-XXXX

# Manual override (admin) — mark a task done without running it
./venv/bin/python plugins/nurture-community/scripts/tasks.py complete task-XXXX

# Garbage collect — mark expired + drop very old non-active
./venv/bin/python plugins/nurture-community/scripts/tasks.py cleanup
```

When community + agent are both running, an agent picks up new tasks on
its next 30s tick.

---

## Kanban dispatcher

The dispatcher spawns a worker hermes process per Kanban card. Run it
periodically (cron) or every time you want to flush pending work:

```bash
# Ensure hermes is on PATH (dispatcher spawns workers as `hermes ...`)
export PATH="$PWD/venv/bin:$PATH"

# Dry-run — see what would spawn
HERMES_HOME=$HOME/.hermes/profiles/agent hermes kanban dispatch --dry-run

# Real dispatch
HERMES_HOME=$HOME/.hermes/profiles/agent hermes kanban dispatch

# Watch live
HERMES_HOME=$HOME/.hermes/profiles/agent hermes kanban list
HERMES_HOME=$HOME/.hermes/profiles/agent hermes kanban log <task_id>
```

Hermes can also embed the dispatcher inside its gateway (60s ticks):
`hermes gateway start` runs the messaging gateway with kanban dispatch
baked in.

---

## LLM setup (required for worker execution)

The worker hermes process is a Pi Agent — it needs an LLM provider to
decide which `nurture_execute` calls to make. Without one, the worker
exits immediately with the message:

```
It looks like Hermes isn't configured yet -- no API keys or providers found.
```

Configure once for the worker profile:

```bash
# Interactive wizard — covers all popular providers
HERMES_HOME=$HOME/.hermes/profiles/nurture-task-worker hermes setup

# Or manually for OpenRouter
HERMES_HOME=$HOME/.hermes/profiles/nurture-task-worker hermes config set \
    model.provider openrouter
HERMES_HOME=$HOME/.hermes/profiles/nurture-task-worker hermes config set \
    model.default anthropic/claude-haiku
echo "OPENROUTER_API_KEY=sk-or-..." >> ~/.hermes/profiles/nurture-task-worker/.env

# Or for Anthropic direct
echo "ANTHROPIC_API_KEY=sk-ant-..." >> ~/.hermes/profiles/nurture-task-worker/.env
HERMES_HOME=$HOME/.hermes/profiles/nurture-task-worker hermes config set \
    model.provider anthropic
HERMES_HOME=$HOME/.hermes/profiles/nurture-task-worker hermes config set \
    model.default claude-opus-4-7
```

The agent profile (Terminal B) also needs an LLM if you want to drive
it interactively via `hermes --profile agent` instead of through Kanban.

---

## Quick health checks

```bash
# Community
curl -s http://127.0.0.1:18790/api/health
# → {"status":"ok","backend":"json",...}

# Community endpoints map
curl -s http://127.0.0.1:18790/
# → JSON service info + all endpoint paths

# Agent's Python device service
curl -s http://127.0.0.1:8600/api/health
# → {"status":"ok","environment":"dev",...}

# Real device list
curl -s http://127.0.0.1:8600/api/devices
# → [{"serial":"...","status":"device","model":"..."}]

# Available operations
curl -s http://127.0.0.1:8600/api/capabilities | python3 -m json.tool

# Real device operation (no LLM needed)
curl -s -X POST http://127.0.0.1:8600/api/execute \
    -H 'Content-Type: application/json' \
    -d '{"operation":"core.wake_screen","device_id":"<SERIAL>","params":{}}'
```

---

## Mapping to OpenClaw

| OpenClaw script | Avatar-Hermes equivalent | Notes |
|---|---|---|
| `start-community.sh` | `start-community.sh` | Same port. OpenClaw used Node Gateway WebSocket; we use FastAPI + MCP HTTP. |
| `start-agent.sh` | `start-agent.sh` | OpenClaw was the OpenClaw Gateway Node TS process. We run the Python device service + community connector directly. |
| `start-python.sh` | `start-python.sh` (debug only) | OpenClaw needed this in normal flow; Avatar-Hermes auto-spawns the Python service from `start-agent.sh`. Use this only when debugging the Python service in isolation. |
| (manual subagent flow) | `hermes kanban dispatch` + `nurture-task-worker` profile | OpenClaw had `subagent.run({sessionKey})` hand-managed; Avatar-Hermes uses Kanban for durable task execution. |

---

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

---

## Multi-device

Each device gets its own profile:

```bash
# Profile A
HERMES_HOME=$HOME/.hermes/profiles/agent-A ./start-example/start-agent.sh
# Profile B
HERMES_HOME=$HOME/.hermes/profiles/agent-B ./start-example/start-agent.sh
```

Each profile gets its own:
- Python device service port (override `plugins.nurture.serverPort` in the profile config)
- Community avatar identity (separate JWT token)
- Workspace (recipes, traces, events isolated per agent)

---

## Connecting device → community

By default `start-agent.sh` runs the device offline. To connect it to a
running `start-community.sh`:

```bash
# Edit ~/.hermes/profiles/agent/config.yaml so plugins.nurture.community.url
# points at the community URL:
hermes --profile agent config set plugins.nurture.community.url \
    "http://127.0.0.1:18790"

# Restart start-agent.sh — on first boot it'll register an avatar with
# the community and immediately start uploading capability/trace/event.
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `start-community.sh` exits with `address already in use` | Another process on `:18790`. Set `NURTURE_COMMUNITY_PORT=18791` and retry. |
| `start-agent.sh` says "Python service did not become healthy" | uiautomator2 / adbutils missing in the venv. Re-run `./setup-hermes.sh` then `pip install -r plugins/nurture/python/requirements.txt`. |
| `/api/devices` returns `[]` | No ADB devices attached. Run `adb devices` outside hermes to confirm. |
| Agent connector logs say "401" | Token expired. Delete `~/.hermes/profiles/agent/nurture/agent/community-token.json` and restart — agent will re-register. |
| Worker spawn fails with "`hermes` executable not found on PATH" | `export PATH="$PWD/venv/bin:$PATH"` before running `hermes kanban dispatch`. |
| Worker exits immediately with "no API keys or providers found" | See "LLM setup" above. |
| Tasks created via CLI not visible to the running server | Old bug — fixed in `001131f6c` (JSON store now mtime-reloads). Pull latest and restart community. |
| `WebSocket / 403` in community log | Old bug — fixed in `c806f64c0` (uvicorn `ws="none"` + root handler). Pull latest. |

---

## Verified flows

| Flow | Status |
|---|---|
| Community starts, all endpoints respond | ✓ |
| Agent starts, registers, polls | ✓ |
| MCP server reachable, agent sees 8 remote tools | ✓ |
| Create task via CLI → agent picks up in 30s | ✓ |
| Agent dispatches to Kanban card | ✓ |
| Kanban dispatcher spawns worker process | ✓ (LLM config needed for execution) |
| Real device op `core.observe_screen` | ✓ (screen captured) |
| Real device op `core.wake_screen` | ✓ (screen lit + unlocked) |
| 33-test pytest suite | ✓ All green |
