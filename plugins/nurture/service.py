"""Python device service lifecycle.

Starts ``plugins/nurture/python/server.py`` as a child process, polls
``/api/health`` until ready, and tears down on exit.

Singleton pattern: hermes can have multiple sessions in one process, but
the device service is shared. We spawn at most one per process lifetime.

Replaces ``extensions/nurture/src/service.ts`` (OpenClaw TS lifecycle).
See ``docs/avatar-hermes/device-agent.md`` 3.3 and
``docs/avatar-hermes/plugin-api-audit.md`` 缺口 4.
"""
from __future__ import annotations

import atexit
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen

from .config import NurtureConfig

logger = logging.getLogger(__name__)

_HEALTH_POLL_INTERVAL_S = 0.5
_HEALTH_POLL_TIMEOUT_S = 30.0

_DEFAULT_GOALS_MD = """# Agent 目标

## 核心目标

1. **提升主人的社交媒体价值** — 增长粉丝、提升互动率、扩大影响力
2. **赚取任务奖励** — 完成社区分发的有偿任务获取报酬
3. **维护账号安全** — 避免违规操作,保持自然行为模式

## 行为原则

- 所有操作必须符合主人的人设定位,不做与人设冲突的事
- 优先执行与人设高度相关的任务
- 拒绝与人设明显冲突的任务(除非任务奖励极高且无风险)
- 保持自然的使用节奏,避免被平台检测为异常

## 任务接受标准

- **必接**:与人设高度一致 + 有助于核心目标
- **可接**:与人设无冲突 + 有任务奖励
- **拒绝**:与人设明显冲突(如美食博主去点赞体育内容)
"""


# Process-level singleton — only one Python device service per hermes
# process, regardless of how many sessions start it. Guarded by the
# module lock to make on_session_start idempotent under concurrent
# session spawns (gateway can fire multiple in parallel).
_proc: Optional[subprocess.Popen] = None
_lock = threading.Lock()
_atexit_registered = False


def _python_dir() -> Path:
    """Return the directory containing server.py."""
    return Path(__file__).parent / "python"


def _ensure_workspace_files(workspace: Path) -> None:
    """Seed the workspace with persona/ dir and goals.md if missing.

    Always runs (even when autoStart is False — the dev script may start
    Python externally and we still need the seed files).
    """
    agent_dir = workspace / "agent"
    persona_dir = agent_dir / "persona"
    goals_path = agent_dir / "goals.md"

    persona_dir.mkdir(parents=True, exist_ok=True)
    if not goals_path.exists():
        goals_path.write_text(_DEFAULT_GOALS_MD, encoding="utf-8")
        logger.info("nurture: created default goals.md at %s", goals_path)


def _wait_for_health(host: str, port: int) -> None:
    """Poll ``/api/health`` until 200 or timeout."""
    url = f"http://{host}:{port}/api/health"
    deadline = time.monotonic() + _HEALTH_POLL_TIMEOUT_S
    last_err: Optional[BaseException] = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2.0) as resp:
                if 200 <= resp.status < 300:
                    return
        except URLError as e:
            last_err = e
        except Exception as e:
            last_err = e
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    raise RuntimeError(
        f"Python device service did not become healthy within "
        f"{_HEALTH_POLL_TIMEOUT_S}s (last error: {last_err})"
    )


def start_python_service(config: NurtureConfig) -> bool:
    """Spawn the Python FastAPI device service if not already running.

    Idempotent — returns True if running (whether we started it or it
    was already up). Returns False if disabled by config.

    Logs Python stdout/stderr asynchronously via background reader threads.
    """
    global _proc, _atexit_registered

    workspace = config.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    _ensure_workspace_files(workspace)

    if not config.enabled or not config.auto_start:
        logger.info(
            "nurture: skipping Python service spawn (enabled=%s autoStart=%s)",
            config.enabled,
            config.auto_start,
        )
        return False

    with _lock:
        if _proc is not None and _proc.poll() is None:
            logger.debug("nurture: Python service already running (pid=%s)", _proc.pid)
            return True

        python_dir = _python_dir()
        server_script = python_dir / "server.py"
        if not server_script.exists():
            logger.error(
                "nurture: server.py missing at %s — Phase 1 lift incomplete?",
                server_script,
            )
            return False

        args = [
            config.python_path,
            str(server_script),
            "--host", config.server_host,
            "--port", str(config.server_port),
        ]
        if config.device_id:
            args.extend(["--device-id", config.device_id])
        if config.workspace:
            args.extend(["--workspace", config.workspace])

        env = os.environ.copy()
        env["APP_ENV"] = config.app_env
        env["NURTURE_WORKSPACE"] = str(workspace)

        logger.info(
            "nurture: starting Python device service on %s:%s (pid will follow)",
            config.server_host,
            config.server_port,
        )

        try:
            _proc = subprocess.Popen(
                args,
                cwd=str(python_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            logger.error("nurture: failed to spawn (%s) — pythonPath=%s", e, config.python_path)
            return False

        # Register exit cleanup once
        if not _atexit_registered:
            atexit.register(stop_python_service)
            _atexit_registered = True

        # Start log reader threads (best-effort, daemon)
        threading.Thread(
            target=_log_pipe, args=(_proc.stdout, logging.INFO),
            daemon=True, name="nurture-py-stdout",
        ).start()
        threading.Thread(
            target=_log_pipe, args=(_proc.stderr, logging.WARNING),
            daemon=True, name="nurture-py-stderr",
        ).start()

    # Health check (outside the lock — can take 30s)
    try:
        _wait_for_health(config.server_host, config.server_port)
        logger.info("nurture: Python device service healthy (pid=%s)", _proc.pid if _proc else "?")
        return True
    except Exception as e:
        logger.error("nurture: health check failed: %s", e)
        stop_python_service()
        return False


def stop_python_service() -> None:
    """Send SIGTERM, wait up to 5s, then SIGKILL."""
    global _proc
    with _lock:
        proc = _proc
        _proc = None

    if proc is None or proc.poll() is not None:
        return

    try:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            logger.warning("nurture: Python service didn't exit on SIGTERM, killing")
            proc.kill()
            proc.wait(timeout=2.0)
    except Exception as e:
        logger.warning("nurture: error stopping Python service: %s", e)


def _log_pipe(pipe, level: int) -> None:
    """Read lines from a subprocess pipe and forward to logger."""
    try:
        for raw in iter(pipe.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                logger.log(level, "[nurture:py] %s", line)
    except Exception:
        pass  # pipe closed or process gone
