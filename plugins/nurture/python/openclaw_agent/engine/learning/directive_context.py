"""Community directive context — reads locally cached directives for
executor and recipe generator integration.

The TypeScript community connector writes directives from the community
to ``NURTURE_WORKSPACE/agent/directives.json``.  This module reads that
file and provides helpers for the Python execution engine to incorporate
community strategy guidance.

Directives influence behavior at two levels:
1. **Executor context** — injected into ``ExecutionContext.directives``
   so individual Operations can adjust (e.g., linger timing, skip conditions).
2. **Recipe generator prompt** — ``build_directive_prompt_section()`` returns
   a text block appended to the LLM prompt so generated recipes account for
   current strategy.
"""

import json
import os
from pathlib import Path
from typing import Any

from openclaw_agent.engine.common.logger import get_logger

logger = get_logger("directive_context")


def _resolve_directives_path() -> Path:
    workspace = os.environ.get(
        "NURTURE_WORKSPACE",
        str(
            Path(__file__).resolve().parent.parent.parent.parent.parent
            / "nurture-workspace"
        ),
    )
    return Path(workspace) / "agent" / "directives.json"


def load_active_directives(app: str | None = None) -> list[dict[str, Any]]:
    """Read locally cached directives, filtering out expired entries.

    Args:
        app: If provided, only return directives for this app.

    Returns:
        List of directive dicts (matching the community DirectiveEntry shape).
    """
    path = _resolve_directives_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    result: list[dict[str, Any]] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        expires = d.get("expiresAt")
        if expires:
            try:
                exp_dt = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                if exp_dt < now:
                    continue
            except (ValueError, TypeError):
                pass
        if app and d.get("app") != app:
            continue
        result.append(d)

    # Sort by priority descending
    result.sort(key=lambda x: x.get("priority", 0), reverse=True)
    return result


def build_directive_prompt_section(
    directives: list[dict[str, Any]],
) -> str:
    """Build a text section describing active directives for LLM prompts.

    Returns an empty string if no directives are active.
    """
    if not directives:
        return ""

    lines = [
        "## Active Community Directives",
        "The following strategy directives are currently active. "
        "Your generated code should respect these guidelines:",
        "",
    ]
    for d in directives:
        dtype = d.get("type", "unknown")
        summary = d.get("summary", "")
        priority = d.get("priority", 5)
        lines.append(f"- [{dtype}] (priority {priority}) {summary}")

        instructions = d.get("instructions", [])
        for inst in instructions:
            action = inst.get("action", "")
            target = inst.get("target", "")
            params = inst.get("params", {})
            params_str = ", ".join(f"{k}={v}" for k, v in params.items())
            lines.append(f"  - {action} → {target} ({params_str})")

    lines.append("")
    return "\n".join(lines)


def get_directive_params(
    directives: list[dict[str, Any]],
    operation_name: str,
) -> dict[str, Any]:
    """Extract merged directive params relevant to a specific operation.

    Scans all active directives for instructions targeting the given
    operation (or wildcard ``*``), and merges their params with later
    (higher-priority) directives overriding earlier ones.
    """
    merged: dict[str, Any] = {}
    # directives are already sorted by priority desc, so iterate in reverse
    # to let higher-priority override
    for d in reversed(directives):
        for inst in d.get("instructions", []):
            target = inst.get("target", "")
            if target == "*" or target == operation_name:
                merged.update(inst.get("params", {}))
    return merged
