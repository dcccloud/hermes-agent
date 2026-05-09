# Langfuse integration for Avatar-Hermes

The `nurture` and `nurture-community` plugins do not own observability —
they ride on Hermes's first-class **Langfuse** plugin
(`plugins/observability/langfuse/`), which already traces every LLM call,
tool invocation, and conversation across the agent runtime.

When Langfuse is enabled in either profile, you automatically get:

- Per-`nurture_execute` tool call as a Langfuse span (with operation id
  and device id in the attributes)
- Per-LLM call (Pi Agent reasoning, Recipe Fusion auxiliary calls)
  with token counts and latency
- Tool errors as Langfuse `level=error` events

## Enable on the device profile

```bash
# In the profile that runs the device-side hermes:
hermes plugins enable observability/langfuse

# Add credentials (pk-lf-... / sk-lf-...) to ~/.hermes/.env:
echo 'HERMES_LANGFUSE_PUBLIC_KEY=pk-lf-...' >> ~/.hermes/.env
echo 'HERMES_LANGFUSE_SECRET_KEY=sk-lf-...' >> ~/.hermes/.env

# Optional environment / release tags
echo 'HERMES_LANGFUSE_ENV=prod-douyin' >> ~/.hermes/.env
echo 'HERMES_LANGFUSE_RELEASE=v0.5' >> ~/.hermes/.env
```

## Enable on the community profile

If you want to trace Recipe Fusion LLM calls too:

```bash
hermes --profile community plugins enable observability/langfuse
# (same env vars in the community profile's .env)
```

## What you get on the dashboard

On the Langfuse traces page, filter by:

- `tool.name = "nurture_execute"` — every device operation
- `tool.name = "nurture_task_report"` — every community task outcome
- `tool.name = "nurture.recipes.get"` — MCP discovery calls
- `model = "<your-recipe-fusion-model>"` — fusion LLM calls

Use the time-series view to compare per-operation latencies before /
after a recipe push, or to correlate spike in error rate with a
specific recipe version.

## Why we don't add a custom span ourselves

Hermes's pre/post hooks already wrap every tool call and LLM call.
Adding a duplicate span from the nurture plugin would just create
nested redundant spans. The boundary contract (boundary-contracts.md
不变量 1) — "runtime is zero LLM in the recipe path" — means most
device operations look like a single Pi Agent → tool span, which is
exactly what Langfuse renders.

If you need more granularity (per-step inside the Python device service
during VLM fallback), open the OpenClaw `engine/learning/event_log.py`
hook and emit Langfuse spans there. That's beyond the Phase 5 MVP.
