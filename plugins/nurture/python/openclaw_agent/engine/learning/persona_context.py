"""Persona context — reads per-platform persona Markdown files and goals
from the workspace for executor and prompt integration.

Personas live at ``NURTURE_WORKSPACE/agent/persona/{app}.md`` (one per
platform).  Goals live at ``NURTURE_WORKSPACE/agent/goals.md`` (shared
across platforms).

Both files are human-editable Markdown — the owner can view and modify
them at any time to adjust the agent's behavior.
"""

import os
from pathlib import Path
from typing import Any

from openclaw_agent.engine.common.logger import get_logger

logger = get_logger("persona_context")


def _resolve_workspace() -> Path:
    workspace = os.environ.get(
        "NURTURE_WORKSPACE",
        str(
            Path(__file__).resolve().parent.parent.parent.parent.parent
            / "nurture-workspace"
        ),
    )
    return Path(workspace)


def _resolve_persona_path(app: str) -> Path:
    return _resolve_workspace() / "agent" / "persona" / f"{app}.md"


def _resolve_goals_path() -> Path:
    return _resolve_workspace() / "agent" / "goals.md"


def load_persona(app: str) -> dict[str, Any] | None:
    """Read persona/{app}.md and return structured data.

    Returns:
        ``{"raw": str, "app": str, "exists": True}`` if file exists,
        ``None`` if no persona file for this app.
    """
    path = _resolve_persona_path(app)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        return {"raw": raw, "app": app, "exists": True}
    except OSError:
        return None


def save_persona(app: str, content: str) -> Path:
    """Write persona Markdown to the workspace.

    Creates the persona directory if needed and returns the written path.
    """
    path = _resolve_persona_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    logger.info("Persona saved for %s at %s", app, path)
    return path


def load_goals() -> str | None:
    """Read goals.md raw content.  Returns None if file does not exist."""
    path = _resolve_goals_path()
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def build_persona_prompt_section(persona: dict[str, Any]) -> str:
    """Build a text section describing the persona for LLM prompts.

    Returns an empty string if persona is None or empty.
    """
    if not persona or not persona.get("raw"):
        return ""

    app = persona.get("app", "unknown")
    raw = persona["raw"]
    return (
        f"## 当前平台人设 ({app})\n\n"
        f"以下是主人在 {app} 平台的人设档案。所有操作都应参考此人设：\n\n"
        f"{raw}\n"
    )


def build_goals_prompt_section(goals: str | None) -> str:
    """Build a text section describing goals for LLM prompts."""
    if not goals:
        return ""

    return (
        "## Agent 目标\n\n"
        "以下是 Agent 的总体目标和行为准则：\n\n"
        f"{goals}\n"
    )
