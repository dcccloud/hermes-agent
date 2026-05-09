"""Configuration model for the nurture plugin.

Loads from ``~/.hermes/config.yaml`` under ``plugins.nurture.*``. See
``docs/avatar-hermes/configuration.md`` for the full schema.

Mirrors ``extensions/nurture/src/types.ts``'s ``NurtureConfig`` 1:1 so
the OpenClaw → Hermes config migration is a pure rename.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_cli.config import cfg_get, load_config
from hermes_constants import get_hermes_home


@dataclass
class CommunityConfig:
    """Subsection: community connection (optional)."""
    url: str = ""
    token: str = ""
    password: str = ""
    poll_interval_caps_ms: int = 60_000
    poll_interval_task_ms: int = 30_000
    # Phase 4: hermes profile that picks up community tasks from kanban.
    # Empty string falls back to "nurture-task-worker".
    worker_profile: str = ""


@dataclass
class NurtureConfig:
    """Top-level nurture plugin configuration."""
    enabled: bool = True
    python_path: str = "python3"
    server_host: str = "127.0.0.1"
    server_port: int = 8600
    auto_start: bool = True
    app_env: str = "dev"
    agent_id: str = ""
    device_id: str = ""
    workspace: str = ""  # empty -> get_hermes_home() / "nurture"
    community: CommunityConfig = field(default_factory=CommunityConfig)

    @property
    def workspace_path(self) -> Path:
        """Resolve the workspace directory.

        Empty config falls back to ``$HERMES_HOME/nurture/`` (profile-aware).
        """
        if self.workspace:
            return Path(self.workspace).expanduser().resolve()
        return get_hermes_home() / "nurture"

    @classmethod
    def load(cls) -> "NurtureConfig":
        """Read ``plugins.nurture`` from ``~/.hermes/config.yaml``.

        Missing keys fall back to dataclass defaults. Invalid values
        (negative ports, non-allowed app_env) silently fall back too — we
        don't want config typos to crash the plugin loader.
        """
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
        section: Dict[str, Any] = cfg_get(cfg, "plugins", "nurture", default={}) or {}

        enabled = _as_bool(section.get("enabled"), default=True)
        python_path = _as_nonempty_str(section.get("pythonPath"), default="python3")
        server_host = _as_nonempty_str(section.get("serverHost"), default="127.0.0.1")
        server_port = _as_positive_int(section.get("serverPort"), default=8600)
        auto_start = _as_bool(section.get("autoStart"), default=True)
        app_env_raw = str(section.get("appEnv") or "dev").strip()
        app_env = app_env_raw if app_env_raw in ("dev", "prod", "test") else "dev"
        agent_id = _as_str_or_empty(section.get("agentId"))
        device_id = _as_str_or_empty(section.get("deviceId"))
        workspace = _as_str_or_empty(section.get("workspace"))

        comm_section: Dict[str, Any] = section.get("community") or {}
        community = CommunityConfig(
            url=_as_str_or_empty(comm_section.get("url")),
            token=_as_str_or_empty(comm_section.get("token")),
            password=_as_str_or_empty(comm_section.get("password")),
            poll_interval_caps_ms=_as_positive_int(
                comm_section.get("pollIntervalCapsMs"), default=60_000, minimum=5_000
            ),
            poll_interval_task_ms=_as_positive_int(
                comm_section.get("pollIntervalTaskMs"), default=30_000, minimum=5_000
            ),
            worker_profile=_as_str_or_empty(comm_section.get("workerProfile")),
        )

        return cls(
            enabled=enabled,
            python_path=python_path,
            server_host=server_host,
            server_port=server_port,
            auto_start=auto_start,
            app_env=app_env,
            agent_id=agent_id,
            device_id=device_id,
            workspace=workspace,
            community=community,
        )


# ---------------------------------------------------------------------------
# Defensive coercion helpers — config can be hand-edited, treat values as
# untrusted and fall back silently rather than crashing the plugin loader.
# ---------------------------------------------------------------------------

def _as_bool(v: Any, *, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    return default


def _as_nonempty_str(v: Any, *, default: str) -> str:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return default


def _as_str_or_empty(v: Any) -> str:
    if isinstance(v, str):
        return v.strip()
    return ""


def _as_positive_int(v: Any, *, default: int, minimum: int = 1) -> int:
    if isinstance(v, int) and not isinstance(v, bool) and v >= minimum:
        return v
    return default
