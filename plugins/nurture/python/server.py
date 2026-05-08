"""
OperationEngine HTTP Server + Gateway Node

轻量 HTTP 接口 + WebSocket node 客户端，为 OpenClaw Agent Gateway 提供设备操作服务。

HTTP 接口保留用于 community-sync 和调试。
Gateway Node 是主要的 Agent 交互通道（通过标准 nodes 工具）。

启动方式:
  cd extensions/nurture/python
  APP_ENV=dev python server.py                            # 仅 HTTP 模式
  APP_ENV=dev python server.py --gateway-url ws://127.0.0.1:18791 --node-id nurture-myhost
"""

import argparse
import asyncio
import json
import shutil
import subprocess
import time
import os
import sys

# adb PATH 兜底探测：conda 环境可能不继承 homebrew/Android SDK 的 PATH
if not shutil.which("adb"):
    for _p in ["/opt/homebrew/bin", "/usr/local/bin",
               os.path.expanduser("~/Android/Sdk/platform-tools"),
               os.path.expanduser("~/Library/Android/sdk/platform-tools")]:
        if os.path.isfile(os.path.join(_p, "adb")):
            os.environ["PATH"] = f"{_p}:{os.environ.get('PATH', '')}"
            break

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional
import uvicorn
import logging


# ---------------------------------------------------------------------------
# Suppress noisy periodic access logs (e.g. /api/capabilities polling)
# ---------------------------------------------------------------------------

class _QuietAccessFilter(logging.Filter):
    """Drop uvicorn access-log lines for high-frequency polling endpoints."""
    _SUPPRESSED = ("/api/capabilities", "/api/health", "/api/traces/pending", "/api/events")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._SUPPRESSED)

logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())


# ---------------------------------------------------------------------------
# 在导入 openclaw_agent.engine 前设置环境
# ---------------------------------------------------------------------------

env = os.environ.get("APP_ENV", "dev")
os.environ["APP_ENV"] = env

from openclaw_agent.engine.common import get_logger, get_config
from openclaw_agent.engine.common.config import reload_config
from openclaw_agent.engine.agents.phone_agent_pool import PhoneAgentPool
from openclaw_agent.engine.core.executor import OperationExecutor
from openclaw_agent.engine.learning.checkpoint_store import (
    CheckpointStore,
    compute_task_fingerprint,
)
import openclaw_agent.engine.operations  # noqa: F401 — 注册所有 operation


logger = get_logger("http_server")

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class OperationItem(BaseModel):
    name: str
    handler_class: str
    display_name: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)


class ExecuteTaskRequest(BaseModel):
    task_id: str
    device_id: str = ""
    operations: List[OperationItem]
    task_type: str = "nurture"


class ExecuteRequest(BaseModel):
    """单个 operation 的简化请求"""
    device_id: str = ""
    operation: str  # handler_class
    params: Dict[str, Any] = Field(default_factory=dict)
    task_id: Optional[str] = None


class OperationResultItem(BaseModel):
    name: str
    status: str
    duration_ms: int
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class TaskResultResponse(BaseModel):
    task_id: str
    task_type: str
    success: bool
    result: Dict[str, Any]


class DeviceInfo(BaseModel):
    serial: str
    status: str
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# App init
# ---------------------------------------------------------------------------

config = reload_config()
logger.info(f"HTTP Server 启动 | 环境: {env}")

agent_pool = PhoneAgentPool(
    base_url=config.autoglm.base_url,
    api_key=config.autoglm.api_key,
    model_name=config.autoglm.model_name,
    max_steps=config.autoglm.max_steps,
    temperature=config.autoglm.temperature,
)
logger.info("PhoneAgent 池初始化完成")

executor = OperationExecutor(agent_pool)
logger.info("OperationExecutor 初始化完成")

# Recipe generation interval (seconds)
_RECIPE_GEN_INTERVAL = int(os.environ.get("RECIPE_GEN_INTERVAL", "1800"))  # 30 min


async def _recipe_generation_loop() -> None:
    """Background task: periodically consume VLM traces and generate recipes."""
    from openclaw_agent.engine.learning.recipe_generator import RecipeGenerator
    gen = RecipeGenerator()
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(_RECIPE_GEN_INTERVAL)
        try:
            count = await loop.run_in_executor(None, gen.process_pending)
            if count:
                logger.info("Recipe generator produced %d new recipe(s)", count)
        except Exception:
            logger.exception("Recipe generation loop error")


# Page discovery interval (seconds) — shorter than recipe since discoveries
# are cheap to check (just reading a JSONL) and valuable to process quickly.
_DISCOVERY_INTERVAL = int(os.environ.get("DISCOVERY_INTERVAL", "300"))  # 5 min


async def _discovery_processing_loop() -> None:
    """Background task: consume pending page discoveries and update app graphs."""
    from openclaw_agent.engine.learning.graph_updater import process_pending_discoveries
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(_DISCOVERY_INTERVAL)
        try:
            count = await loop.run_in_executor(None, process_pending_discoveries)
            if count:
                logger.info("Discovery loop found %d new page(s)", count)
        except Exception:
            logger.exception("Discovery processing loop error")


# ---------------------------------------------------------------------------
# Gateway Node command handler (shared by HTTP endpoints and WS node)
# ---------------------------------------------------------------------------

def _handle_node_command(command: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dispatch a nurture.* command and return the result dict.

    This is the shared handler used by both the Gateway Node (WebSocket) and
    can be called from HTTP endpoints for consistency.
    """
    if command == "nurture.health":
        return {"status": "ok", "environment": env, "timestamp": int(time.time())}

    if command == "nurture.devices.list":
        return _cmd_list_devices()

    if command == "nurture.execute":
        return _cmd_execute_single(
            device_id=_resolve_device_id(params.get("device_id", "")),
            operation=params.get("operation", ""),
            params=params.get("params", {}),
            task_id=params.get("task_id"),
            fresh=bool(params.get("fresh")),
        )

    if command == "nurture.execute_task":
        device_id = _resolve_device_id(params.get("device_id", ""))
        operations = params.get("operations", [])
        task_id, is_continuation = _resolve_task_identity(
            params, device_id, operations, CheckpointStore(),
        )
        return _cmd_execute_task(
            task_id=task_id,
            device_id=device_id,
            operations=operations,
            task_type=params.get("task_type", "nurture"),
            is_continuation=is_continuation,
        )

    if command == "nurture.capabilities":
        return get_capabilities()

    if command == "nurture.recipes.list":
        return _cmd_list_recipes(params.get("operation"))

    if command == "nurture.recipes.get":
        return _cmd_get_recipe(
            params.get("app", ""),
            params.get("operation", ""),
            params.get("step", "main"),
        )

    if command == "nurture.recipes.generate":
        return _cmd_trigger_recipe_generation()

    if command == "nurture.recipes.import":
        return _cmd_import_recipe(params)

    if command == "nurture.graph.get":
        return _cmd_get_app_graph(params.get("app", ""))

    if command == "nurture.graph.merge":
        return _cmd_merge_app_graph(params.get("app", ""), params.get("pages", []))

    raise ValueError(f"Unknown command: {command}")


def _cmd_list_devices() -> Dict[str, Any]:
    try:
        result = subprocess.run(
            ["adb", "devices", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        devices = []
        for line in result.stdout.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) >= 2:
                serial, status = parts[0], parts[1]
                model = None
                for part in parts[2:]:
                    if part.startswith("model:"):
                        model = part.split(":", 1)[1]
                devices.append({"serial": serial, "status": status, "model": model})
        return {"devices": devices}
    except FileNotFoundError:
        return {"devices": [], "error": "adb not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"devices": [], "error": "adb devices timed out"}


def _cmd_execute_single(
    device_id: str, operation: str, params: Dict[str, Any],
    task_id: Optional[str] = None,
    fresh: bool = False,
) -> Dict[str, Any]:
    if not operation:
        raise ValueError(
            "Missing required parameter 'operation'. "
            "Expected: {\"device_id\": \"<serial>\", \"operation\": \"douyin.enter_recommend_feed\", \"params\": {}}"
        )
    if not device_id:
        raise ValueError(
            "Missing required parameter 'device_id'. "
            "Use nurture.devices.list to find available device serials."
        )
    handler_class = operation
    params = dict(params) if params else {}

    if "." in handler_class and not any(
        c.isupper() for c in handler_class.split(".")[-1][:1]
    ):
        from openclaw_agent.engine.learning.recipe_store import RecipeStore
        parts = handler_class.split(".", 1)
        if len(parts) == 2:
            recipe_op = f"{parts[0]}/{parts[1]}"
            store = RecipeStore()
            if store.get(recipe_op, params.get("recipe_step", "main")):
                handler_class = "operations.core.dynamic_recipe.DynamicRecipeOperation"
                params["recipe_operation"] = recipe_op
            else:
                resolved = _resolve_builtin_handler_from_command(handler_class)
                if resolved:
                    handler_class = resolved

    req_op = operation
    if "." in req_op and req_op.count(".") == 1:
        app_part, op_name = req_op.split(".", 1)
    else:
        app_part = None
        op_name = handler_class.rsplit(".", 1)[-1] if "." in handler_class else handler_class

    # Look up full YAML config for this operation so state_checks are
    # passed through to the executor (precondition / postcondition).
    yaml_config: Dict[str, Any] = {}
    if app_part:
        for op_cfg in _load_operations_yaml().get(app_part, []):
            if op_cfg.get("name") == op_name:
                yaml_config = op_cfg
                break

    # Inject target-app context (affiliated_app + app_package) into params
    # under reserved underscore-prefixed keys. Handlers like
    # GenericVlmTaskOperation use these to enforce the app boundary —
    # ensuring the task is performed inside the right app even when the
    # caller didn't first call <app>.open_app or other apps share the
    # foreground. Reserved-underscore prefix keeps these out of the
    # LLM-facing parameter schema.
    target_app = yaml_config.get("affiliated_app")
    target_app_package = yaml_config.get("app_package")
    if isinstance(target_app, str) and target_app:
        params.setdefault("_target_app", target_app)
    if isinstance(target_app_package, str) and target_app_package:
        params.setdefault("_target_app_package", target_app_package)

    op_entry: Dict[str, Any] = {
        "name": op_name,
        "handler_class": handler_class,
        "display_name": yaml_config.get("display_name", op_name),
        "params": params,
    }
    # Merge YAML-level keys the executor needs (state_checks, etc.)
    if yaml_config.get("state_checks"):
        op_entry["state_checks"] = yaml_config["state_checks"]

    # Resolve task identity via fingerprint reuse so a single-op retry
    # within the TTL window inherits the prior in-flight VLM trace.
    resolved_task_id, is_continuation = _resolve_task_identity(
        {"task_id": task_id, "fresh": fresh},
        device_id, [op_entry], CheckpointStore(),
    )
    logger.info(
        "执行操作 device=%s op=%s task_id=%s is_continuation=%s",
        device_id, operation, resolved_task_id, is_continuation,
    )
    task_message = {
        "task_id": resolved_task_id,
        "task_type": "single",
        "device_id": device_id,
        "operations": [op_entry],
        "is_continuation": is_continuation,
    }
    return executor.execute_task(task_message)


def _cmd_execute_task(
    task_id: str, device_id: str,
    operations: List[Dict[str, Any]], task_type: str = "nurture",
    is_continuation: bool = False,
) -> Dict[str, Any]:
    if not device_id:
        raise ValueError(
            "Missing required parameter 'device_id'. "
            "Use nurture.devices.list to find available device serials."
        )
    if not operations:
        raise ValueError(
            "Missing required parameter 'operations'. "
            "Expected: [{\"name\": \"...\", \"handler_class\": \"...\", \"params\": {}}]"
        )
    logger.info(
        "执行任务 task_id=%s device=%s ops=%d is_continuation=%s",
        task_id, device_id, len(operations), is_continuation,
    )
    yaml_ops = _load_operations_yaml()
    enriched: List[Dict[str, Any]] = []
    for op in operations:
        entry: Dict[str, Any] = {
            "name": op.get("name", ""),
            "handler_class": op.get("handler_class", ""),
            "display_name": op.get("display_name") or op.get("name", ""),
            "params": op.get("params", {}),
        }
        # Merge YAML state_checks so executor can run pre/postcondition checks
        op_name = entry["name"]
        for app_ops in yaml_ops.values():
            for cfg in app_ops:
                if cfg.get("name") == op_name and cfg.get("state_checks"):
                    entry["state_checks"] = cfg["state_checks"]
                    break
        enriched.append(entry)
    task_message = {
        "task_id": task_id,
        "task_type": task_type,
        "device_id": device_id,
        "operations": enriched,
        "is_continuation": is_continuation,
    }
    return executor.execute_task(task_message)


def _resolve_task_identity(
    params: Dict[str, Any],
    device_id: str,
    operations: List[Dict[str, Any]],
    store: CheckpointStore,
) -> tuple[str, bool]:
    """Resolve ``(task_id, is_continuation)`` for an execute_task request.

    Resolution order:
      1. ``params["fresh"]`` truthy → always mint a new task_id (non-continuation)
      2. ``params["task_id"]`` explicit → reuse it; ``is_continuation`` iff prior
         checkpoints exist for that task_id
      3. fingerprint match within TTL → reuse the prior task_id (continuation)
      4. otherwise → mint a new task_id and register it

    The ``store`` is passed in (rather than constructed internally) so the
    workspace-scoped data dir patched by ``_configure_workspace`` is
    honored: the caller instantiates ``CheckpointStore()`` after that
    patch has taken effect.
    """
    cfg = get_config().resume
    if not cfg.enabled:
        explicit = params.get("task_id")
        return explicit or f"task-{int(time.time() * 1000)}", False

    if params.get("fresh"):
        new_id = f"task-{int(time.time() * 1000)}"
        fp = compute_task_fingerprint(device_id, operations)
        store.register_task(new_id, fp, device_id, len(operations))
        logger.info("Task identity (fresh): task_id={} fp={}", new_id, fp)
        return new_id, False

    explicit = params.get("task_id")
    if explicit:
        prior = store.get_task(explicit)
        if not prior:
            fp = compute_task_fingerprint(device_id, operations)
            store.register_task(explicit, fp, device_id, len(operations))
            logger.info("Task identity (explicit, new): task_id={} fp={}", explicit, fp)
            return explicit, False
        logger.info(
            "Task identity (explicit, continuation): task_id={} prior_ops={}",
            explicit, len(prior),
        )
        return explicit, True

    fp = compute_task_fingerprint(device_id, operations)
    reused = store.find_by_fingerprint(
        fp,
        max_age_seconds=cfg.fingerprint_ttl_seconds,
        device_id=device_id,
    )
    if reused:
        logger.info(
            "Task identity (fingerprint reuse): task_id={} fp={} ttl={}s",
            reused, fp, cfg.fingerprint_ttl_seconds,
        )
        return reused, True

    new_id = f"task-{int(time.time() * 1000)}"
    store.register_task(new_id, fp, device_id, len(operations))
    logger.info("Task identity (new): task_id={} fp={}", new_id, fp)
    return new_id, False


def _cmd_list_recipes(operation: Optional[str] = None) -> Any:
    from openclaw_agent.engine.learning.recipe_store import RecipeStore
    return RecipeStore().list_recipes(operation)


def _cmd_get_recipe(app: str, operation: str, step: str) -> Dict[str, Any]:
    from openclaw_agent.engine.learning.recipe_store import RecipeStore
    store = RecipeStore()
    full_op = f"{app}/{operation}" if operation else app
    recipe = store.get(full_op, step)
    if not recipe:
        return {"error": "Recipe not found"}
    code = recipe.path.read_text(encoding="utf-8")
    meta = store._read_meta(full_op, step)
    return {"operation": full_op, "step": step, "code": code, "meta": meta}


def _cmd_trigger_recipe_generation() -> Dict[str, Any]:
    from openclaw_agent.engine.learning.recipe_generator import RecipeGenerator
    return {"generated": RecipeGenerator().process_pending()}


def _cmd_import_recipe(params: Dict[str, Any]) -> Dict[str, Any]:
    from openclaw_agent.engine.learning.recipe_generator import _validate_code
    from openclaw_agent.engine.learning.recipe_store import RecipeStore

    code = params.get("code", "")
    valid, reason = _validate_code(code)
    if not valid:
        return {"imported": False, "error": f"Recipe validation failed: {reason}"}

    store = RecipeStore()
    full_op = f"{params.get('app', '')}/{params.get('operation', '')}"
    step = params.get("step", "main")
    store.save(full_op, step, code)
    logger.info(f"Imported recipe {full_op}/{step} from {params.get('origin', 'unknown')}")
    return {"imported": True, "operation": full_op, "step": step}


def _cmd_get_app_graph(app_name: str) -> Dict[str, Any]:
    from openclaw_agent.engine.learning.app_graph import get_app_graph as _get
    graph = _get(app_name)
    pages = graph.get_pages()
    return {
        "app": app_name,
        "initial_state": graph.initial_state,
        "pages": {pid: s.to_dict() for pid, s in pages.items()},
    }


def _cmd_merge_app_graph(app_name: str, pages_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    from openclaw_agent.engine.learning.app_graph import get_app_graph as _get
    graph = _get(app_name)
    merged_count = 0
    rejected_count = 0
    for page_data in pages_data:
        state_id = page_data.get("state_id", "")
        if not state_id:
            continue
        source = page_data.get("source")
        if source is not None and source != "discovered":
            continue
        if source == "discovered":
            verification = page_data.get("verification", "unverified")
            if verification != "verified":
                rejected_count += 1
                continue
        raw_indicators = page_data.get("indicators", [])
        indicators = []
        for i in raw_indicators:
            if isinstance(i, dict) and "text" in i:
                indicators.append(i["text"])
            elif isinstance(i, str):
                indicators.append(i)
        graph.register_state(
            state_id=state_id,
            name=page_data.get("name", state_id),
            description=page_data.get("description", ""),
            indicators=indicators,
            discovery_path=page_data.get("discovery_path", []),
            is_optional=page_data.get("is_optional", False),
        )
        merged_count += 1
    return {"merged": merged_count, "rejected": rejected_count}


# ---------------------------------------------------------------------------
# Gateway Node client instance (initialized in lifespan if configured)
# ---------------------------------------------------------------------------

_gateway_node = None


@asynccontextmanager
async def lifespan(a: FastAPI):
    global _gateway_node
    tasks = []

    # Event log retention pruning
    from openclaw_agent.engine.learning.event_log import get_event_log
    pruned = get_event_log().retention_prune()
    if pruned:
        logger.info("Pruned %d old event log file(s)", pruned)

    # Recipe generation background task
    tasks.append(asyncio.create_task(_recipe_generation_loop()))
    logger.info(
        "Recipe generation background task started (interval=%ds)",
        _RECIPE_GEN_INTERVAL,
    )

    # Page discovery background task
    tasks.append(asyncio.create_task(_discovery_processing_loop()))
    logger.info(
        "Discovery processing background task started (interval=%ds)",
        _DISCOVERY_INTERVAL,
    )

    # Gateway Node (if configured)
    gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL", "")
    node_id = os.environ.get("OPENCLAW_NODE_ID", "")
    gateway_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")

    if gateway_url and node_id:
        # Bridge stdlib logging (used by gateway_node) to loguru so messages are visible
        import logging as _stdlib_logging
        _gw_logger = _stdlib_logging.getLogger("openclaw_agent.gateway_node")
        _gw_logger.setLevel(_stdlib_logging.DEBUG)
        if not _gw_logger.handlers:
            _handler = _stdlib_logging.StreamHandler()
            _handler.setFormatter(_stdlib_logging.Formatter("%(levelname)s [gateway_node] %(message)s"))
            _gw_logger.addHandler(_handler)

        from openclaw_agent.gateway_node import GatewayNodeClient
        _gateway_node = GatewayNodeClient(
            url=gateway_url,
            node_id=node_id,
            token=gateway_token,
            command_handler=_handle_node_command,
            capability_provider=get_capabilities,
        )
        tasks.append(asyncio.create_task(_gateway_node.run()))
        logger.info(f"Gateway node starting: {gateway_url} as {node_id}")
    else:
        logger.info("Gateway node disabled (OPENCLAW_GATEWAY_URL / OPENCLAW_NODE_ID not set)")

    yield

    if _gateway_node:
        _gateway_node.stop()
    for t in tasks:
        t.cancel()


app = FastAPI(
    title="OperationEngine HTTP Worker",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/health")
async def health():
    return {"status": "ok", "environment": env, "timestamp": int(time.time())}


@app.get("/api/devices")
async def list_devices():
    """列出 adb 已连接的设备"""
    result = _cmd_list_devices()
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])
    return result["devices"]


def _resolve_device_id(device_id: str) -> str:
    """Resolve device_id: use request value, fall back to bound device."""
    return device_id or _bound_device_id or ""


def _execute_error_detail(exc: BaseException) -> str:
    """Format an exception so the bridge / LLM sees the actual cause.

    Default FastAPI 500 returns an empty body which makes "device not
    online" / config / VLM errors invisible to the agent. Surfacing the
    type + message keeps the HTTP layer honest.
    """
    msg = str(exc).strip() or repr(exc)
    return f"{type(exc).__name__}: {msg}"


@app.post("/api/execute_task", response_model=TaskResultResponse)
async def execute_task(req: ExecuteTaskRequest):
    """执行一组 operations（完整任务）"""
    try:
        result = _cmd_execute_task(
            task_id=req.task_id, device_id=_resolve_device_id(req.device_id),
            operations=[op.model_dump() for op in req.operations],
            task_type=req.task_type,
        )
    except HTTPException:
        raise
    except ValueError as e:
        # Caller-side input error (missing required field, etc.)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("execute_task failed: %s", e)
        raise HTTPException(status_code=500, detail=_execute_error_detail(e)) from e
    return TaskResultResponse(**result)


@app.post("/api/execute", response_model=TaskResultResponse)
async def execute_single(req: ExecuteRequest):
    """执行单个 operation（自动包装为 task）"""
    try:
        result = _cmd_execute_single(
            device_id=_resolve_device_id(req.device_id), operation=req.operation,
            params=dict(req.params) if req.params else {},
            task_id=req.task_id,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("execute_single failed: %s", e)
        raise HTTPException(status_code=500, detail=_execute_error_detail(e)) from e
    return TaskResultResponse(**result)


# ---------------------------------------------------------------------------
# Capabilities (dynamic capability manifest for Gateway registration)
# ---------------------------------------------------------------------------

def _compute_capability_version() -> str:
    """Content-based version stamp for capability change detection."""
    import hashlib
    from openclaw_agent.engine.learning.recipe_store import RecipeStore
    store = RecipeStore()
    recipes = store.list_recipes()
    blob = json.dumps(recipes, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _load_operations_yaml() -> Dict[str, List[Dict[str, Any]]]:
    """Load operations from YAML configs, grouped by app."""
    import yaml
    from pathlib import Path
    ops_dir = Path(__file__).resolve().parent / "config" / "operations"
    result: Dict[str, List[Dict[str, Any]]] = {}
    if not ops_dir.exists():
        return result
    for yf in sorted(ops_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
            for op in data.get("operations", []):
                app = op.get("affiliated_app", yf.stem)
                result.setdefault(app, []).append(op)
        except Exception:
            pass
    return result


def _resolve_builtin_handler_from_command(handler_class: str) -> Optional[str]:
    """
    Gateway / node.invoke often sends ``app.operation_name`` (e.g. douyin.enter_recommend_feed).
    Executor expects ``operations.<app>.<module>.<ClassName>`` from YAML.

    Returns the full handler_class if found, else None.
    """
    parts = handler_class.split(".")
    if len(parts) != 2:
        return None
    app, op_name = parts[0], parts[1]
    if not app or not op_name:
        return None
    for op in _load_operations_yaml().get(app, []):
        if op.get("name") == op_name:
            hc = op.get("handler_class")
            return hc if isinstance(hc, str) and hc else None
    return None


@app.get("/api/capabilities")
def get_capabilities():
    """Return full capability manifest with recipe health and graph summary."""
    from openclaw_agent.engine.learning.recipe_store import RecipeStore
    from openclaw_agent.engine.learning.app_graph import get_app_graph as _get

    store = RecipeStore()
    all_recipes = store.list_recipes()
    recipe_map: Dict[str, Dict[str, Any]] = {}
    for r in all_recipes:
        key = f"{r['operation']}/{r['step']}"
        recipe_map[key] = r

    ops_by_app = _load_operations_yaml()
    apps: Dict[str, Any] = {}

    for app, ops in ops_by_app.items():
        op_list = []
        for op in ops:
            op_name = op.get("name", "")
            handler = op.get("handler_class", "")
            command_id = f"{app}.{op_name}"

            steps_info = []
            prefix = f"{app}/{op_name}"
            for rk, rv in recipe_map.items():
                if rk.startswith(prefix + "/") or rk == prefix:
                    s = rv.get("success", 0)
                    f = rv.get("failure", 0)
                    total = s + f
                    code = ""
                    recipe_obj = store.get(rv.get("operation", ""), rv.get("step", ""))
                    if recipe_obj and recipe_obj.path.exists():
                        try:
                            code = recipe_obj.path.read_text(encoding="utf-8")
                        except Exception:
                            pass
                    steps_info.append({
                        "step": rv.get("step", ""),
                        "has_recipe": True,
                        "success": s,
                        "failure": f,
                        "success_rate": round(s / total, 3) if total > 0 else 0.0,
                        "disabled": rv.get("disabled", False),
                        "code": code,
                    })

            op_entry: Dict[str, Any] = {
                "id": command_id,
                "name": op_name,
                "handler_class": handler,
                "display_name": op.get("display_name", op_name),
                "description": op.get("description", ""),
                "has_recipe": any(si["has_recipe"] for si in steps_info),
                "steps": steps_info,
            }
            if op.get("parameters"):
                op_entry["parameters"] = op["parameters"]
            op_list.append(op_entry)

        graph_summary: Dict[str, Any] = {}
        try:
            graph = _get(app)
            pages = graph.get_pages()
            learned = sum(1 for p in pages.values() if p.source == "discovered")
            verified = sum(1 for p in pages.values() if p.is_verified)
            unverified = sum(
                1 for p in pages.values()
                if p.source == "discovered" and not p.is_verified
                and not p.is_disabled)
            disabled = sum(1 for p in pages.values() if p.is_disabled)
            # Collect verified discovered pages for cross-node consensus
            verified_pages = []
            for p in pages.values():
                if p.source == "discovered" and p.is_verified:
                    verified_pages.append({
                        "state_id": p.state_id,
                        "name": p.name,
                        "description": p.description,
                        "indicators": [i["text"] for i in p.indicators],
                        "discovery_path": p.discovery_path,
                        "is_optional": p.is_optional,
                        "verification": p.verification,
                        "seen_count": p.seen_count,
                        "transitions": {
                            k: v for k, v in p.transitions.items()
                        } if p.transitions else {},
                    })
            # Collect all transitions for full graph topology
            all_transitions: Dict[str, Dict[str, str]] = {}
            for p in pages.values():
                if p.transitions:
                    all_transitions[p.state_id] = dict(p.transitions)
            graph_summary = {
                "page_count": len(pages),
                "learned_count": learned,
                "verified_count": verified,
                "unverified_count": unverified,
                "disabled_count": disabled,
                "verified_discovered_pages": verified_pages,
                "transitions": all_transitions,
            }
        except Exception:
            pass

        apps[app] = {
            "operations": op_list,
            "graph_summary": graph_summary,
        }

    # -- Dynamic operations from standalone learned recipes -----------------
    registered_ops: set[str] = set()
    for a, ops in ops_by_app.items():
        for op in ops:
            registered_ops.add(f"{a}/{op.get('name', '')}")

    seen_dynamic: set[str] = set()
    for r in all_recipes:
        op_path = r["operation"]  # e.g. "douyin/go_to_messages"
        parts = op_path.split("/", 1)
        if len(parts) < 2:
            continue
        r_app, r_name = parts[0], parts[1]

        # Skip recipes that belong to a YAML-registered operation
        if op_path in registered_ops:
            continue
        # Also skip sub-steps of registered ops (e.g. enter_data_center/go_to_me_tab)
        parent_op = f"{r_app}/{r_name.split('/')[0]}"
        if parent_op in registered_ops:
            continue
        if op_path in seen_dynamic:
            continue
        seen_dynamic.add(op_path)

        meta = store.read_meta(op_path, r["step"])
        s = r.get("success", 0)
        f = r.get("failure", 0)
        total = s + f
        code = ""
        recipe_obj = store.get(op_path, r["step"])
        if recipe_obj and recipe_obj.path.exists():
            try:
                code = recipe_obj.path.read_text(encoding="utf-8")
            except Exception:
                pass

        dyn_op = {
            "id": f"{r_app}.{r_name}",
            "name": r_name,
            "handler_class": "operations.core.dynamic_recipe.DynamicRecipeOperation",
            "display_name": meta.get("display_name", r_name),
            "has_recipe": True,
            "source": "learned",
            "steps": [{
                "step": r["step"],
                "has_recipe": True,
                "success": s,
                "failure": f,
                "success_rate": round(s / total, 3) if total > 0 else 0.0,
                "disabled": r.get("disabled", False),
                "code": code,
            }],
        }
        apps.setdefault(r_app, {"operations": [], "graph_summary": {}})
        apps[r_app]["operations"].append(dyn_op)

    return {
        "version": _compute_capability_version(),
        "apps": apps,
    }


# ---------------------------------------------------------------------------
# Learning / Recipe endpoints
# ---------------------------------------------------------------------------


@app.post("/api/recipes/generate")
async def trigger_recipe_generation():
    return _cmd_trigger_recipe_generation()


@app.post("/api/discoveries/process")
async def trigger_discovery_processing(app: Optional[str] = None):
    from openclaw_agent.engine.learning.graph_updater import process_pending_discoveries
    import functools
    loop = asyncio.get_running_loop()
    count = await loop.run_in_executor(
        None, functools.partial(process_pending_discoveries, app=app),
    )
    return {"discovered": count}


@app.get("/api/recipes")
async def list_recipes(operation: Optional[str] = None):
    return _cmd_list_recipes(operation)


@app.get("/api/recipes/{app_name}/{operation:path}/{step}")
async def get_recipe_code(app_name: str, operation: str, step: str):
    result = _cmd_get_recipe(app_name, operation, step)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/traces/pending")
async def list_pending_traces(
    operation: Optional[str] = None,
    limit: int = 20,
    include_consumed: bool = False,
    outcomes: Optional[str] = None,
):
    from openclaw_agent.engine.learning.trace_store import TraceStore
    outcome_list = outcomes.split(",") if outcomes else None
    return TraceStore().get_pending(
        operation=operation,
        limit=limit,
        include_consumed=include_consumed,
        outcomes=outcome_list if outcome_list else None,
    )


# ---------------------------------------------------------------------------
# Event log (unified chronological event stream)
# ---------------------------------------------------------------------------


@app.get("/api/events")
async def list_events(
    since: Optional[str] = None,
    until: Optional[str] = None,
    event_types: Optional[str] = None,
    task_id: Optional[str] = None,
    operation: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 100,
):
    from openclaw_agent.engine.learning.event_log import get_event_log

    types_list = event_types.split(",") if event_types else None
    events, next_cursor = get_event_log().query(
        since=since,
        until=until,
        event_types=types_list,
        task_id=task_id,
        operation=operation,
        cursor=cursor,
        limit=limit,
    )
    return {
        "events": events,
        "cursor": next_cursor,
        "has_more": next_cursor is not None,
    }


# ---------------------------------------------------------------------------
# Recipe import (receive recipe from server/other nodes)
# ---------------------------------------------------------------------------


class RecipeImportRequest(BaseModel):
    app: str
    operation: str
    step: str
    code: str
    version: str = ""
    origin: str = ""
    global_success_rate: float = 0.0


@app.post("/api/recipes/import")
async def import_recipe(req: RecipeImportRequest):
    result = _cmd_import_recipe(req.model_dump())
    if not result.get("imported"):
        raise HTTPException(status_code=422, detail=result.get("error", "import failed"))
    return result


# ---------------------------------------------------------------------------
# Graph merge (receive graph updates from server/other nodes)
# ---------------------------------------------------------------------------


class GraphMergeRequest(BaseModel):
    pages: List[Dict[str, Any]]


@app.post("/api/graph/{app_name}/merge")
async def merge_app_graph(app_name: str, req: GraphMergeRequest):
    return _cmd_merge_app_graph(app_name, req.pages)


# ---------------------------------------------------------------------------
# App Graph (page state graph)
# ---------------------------------------------------------------------------


@app.get("/api/graph/{app_name}")
def get_app_graph(app_name: str):
    return _cmd_get_app_graph(app_name)


# ---------------------------------------------------------------------------
# Persona & Goals endpoints
# ---------------------------------------------------------------------------


@app.get("/api/persona/{app_name}")
async def get_persona(app_name: str):
    """Read persona Markdown for a platform.  Returns {exists, raw, app}."""
    from openclaw_agent.engine.learning.persona_context import load_persona
    persona = load_persona(app_name)
    if persona:
        return persona
    return {"exists": False, "raw": "", "app": app_name}


@app.post("/api/persona/{app_name}/init")
async def init_persona(app_name: str):
    """Initialize persona by running scrape operations and generating profile via LLM.

    This is a heavyweight endpoint — it executes multiple device operations to
    collect account data, then calls the LLM to synthesize a persona Markdown.
    """
    from openclaw_agent.engine.learning.persona_context import (
        load_persona,
        save_persona,
    )

    # If persona already exists, return it directly
    existing = load_persona(app_name)
    if existing:
        return existing

    # Collect account data by running scrape operations
    device_id = _resolve_device_id("")
    if not device_id:
        raise HTTPException(
            status_code=400,
            detail="No device available.  Connect a device first.",
        )

    collected: Dict[str, Any] = {}
    scrape_ops = [
        f"{app_name}.view_my_profile",
        f"{app_name}.scrape_overview",
        f"{app_name}.scrape_fan_analysis",
        f"{app_name}.scrape_content_analysis",
    ]
    for op in scrape_ops:
        try:
            logger.info("Persona init: executing %s on device %s", op, device_id)
            # Run blocking device operation in a thread so we don't freeze
            # the uvicorn event loop (health/capability endpoints stay responsive).
            result = await asyncio.to_thread(
                _cmd_execute_single,
                device_id=device_id,
                operation=op,
                params={},
            )
            success = result.get("success", False)
            logger.info("Persona init: %s result success=%s", op, success)
            ops_result = result.get("result", {}).get("operations_result", [])
            for r in ops_result:
                data = r.get("data")
                logger.info(
                    "Persona init: %s op_result status=%s data_type=%s data_preview=%s",
                    op, r.get("status"), type(data).__name__,
                    str(data)[:200] if data else "None",
                )
                if r.get("status") != "success" or not data:
                    continue
                if isinstance(data, dict):
                    # Skip all-null dicts (operation "succeeded" but extracted nothing)
                    if all(v is None for v in data.values()):
                        logger.warning("Persona init: %s returned all-null data, skipping", op)
                        continue
                    collected[op] = data
                elif isinstance(data, str) and len(data) > 20:
                    # VLM finish messages contain useful observations — keep them.
                    # Strip the finish(message="...") wrapper if present.
                    text = data
                    if text.startswith('finish(message="') and text.endswith('")'):
                        text = text[len('finish(message="'):-len('")')]
                    elif text.startswith("finish(message='") and text.endswith("')"):
                        text = text[len("finish(message='"):-len("')")]
                    collected[op] = {"_vlm_observation": text}
                    logger.info("Persona init: %s collected VLM observation (%d chars)", op, len(text))
        except Exception as e:
            logger.warning("Persona init: %s failed — %s", op, e)

    # Also check the task result's top-level message for VLM observations
    # (some operations return the finish message outside the data field)
    logger.info("Persona init: collected data for %d/%d ops: %s",
                len(collected), len(scrape_ops), list(collected.keys()))

    if not collected:
        raise HTTPException(
            status_code=500,
            detail="Failed to collect any account data for persona initialization. "
                   "All scrape operations returned empty or invalid data.",
        )

    # Use LLM to generate persona Markdown
    from datetime import date

    # Build data summary: structured fields + VLM observations
    data_parts: list[str] = []
    for op_name, op_data in collected.items():
        short_name = op_name.split(".")[-1] if "." in op_name else op_name
        if isinstance(op_data, dict) and "_vlm_observation" in op_data:
            data_parts.append(f"### {short_name}\n（屏幕观察）\n{op_data['_vlm_observation']}")
        else:
            data_parts.append(f"### {short_name}\n```json\n{json.dumps(op_data, ensure_ascii=False, indent=2)}\n```")
    data_text = "\n\n".join(data_parts)

    prompt = (
        f"你是一个社交媒体分析专家。根据以下从 {app_name} 账号采集的数据，"
        f"生成一份人设档案（Markdown 格式）。\n\n"
        f"数据来源包括结构化数据和 VLM agent 的屏幕观察记录，"
        f"请从中提取所有有用信息。\n\n"
        f"## 采集数据\n\n{data_text}\n\n"
        f"---\n\n"
        f"请严格按以下格式输出纯 Markdown（不要输出 ```markdown 代码块标记，不要输出 finish() 函数调用）:\n\n"
        f"# {app_name} 人设\n\n"
        f"> 此文件由 Agent 在 {date.today().isoformat()} 自动生成。\n"
        f"> 你可以随时编辑此文件来调整 Agent 的行为方向。\n\n"
        f"## 账号概况\n\n- 用户名: ...\n- 粉丝数: ...\n（等）\n\n"
        f"## 内容定位\n\n- 主要领域: ...\n（等）\n\n"
        f"## 粉丝画像\n\n- 性别分布: ...\n（等）\n\n"
        f"## 互动偏好\n\n- 喜欢的内容类型: ...\n（等）\n\n"
        f"## 当前阶段目标\n\n- ...\n"
    )

    try:
        from openclaw_agent.engine.common import get_config
        cfg = get_config()
        import httpx

        def _llm_call() -> str:
            r = httpx.post(
                cfg.autoglm.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {cfg.autoglm.api_key}"},
                json={
                    "model": cfg.autoglm.model_name,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                },
                timeout=60,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

        persona_md = await asyncio.to_thread(_llm_call)
    except Exception as e:
        logger.warning("Persona init LLM call failed: %s — generating basic template", e)
        # Fallback: generate a minimal template from raw data
        persona_md = (
            f"# {app_name} 人设\n\n"
            f"> 此文件由 Agent 在 {date.today().isoformat()} 自动生成（LLM 不可用，仅包含原始数据）。\n"
            f"> 请手动完善此人设。\n\n"
            f"## 采集到的原始数据\n\n"
            f"```json\n{json.dumps(collected, ensure_ascii=False, indent=2)}\n```\n"
        )

    # Validate: persona must look like Markdown (has heading), not VLM finish output
    if not persona_md.strip().startswith("#") or "finish(message=" in persona_md:
        logger.warning("Persona init: generated content doesn't look like valid persona Markdown, rejecting")
        raise HTTPException(
            status_code=500,
            detail="Generated persona content is invalid (not Markdown). Raw scrape data may be insufficient.",
        )

    logger.info("Persona init: saving persona for %s (%d chars)", app_name, len(persona_md))
    save_persona(app_name, persona_md)
    logger.info("Persona init: %s complete", app_name)
    return {"exists": True, "raw": persona_md, "app": app_name}


@app.get("/api/goals")
async def get_goals():
    """Read goals.md content."""
    from openclaw_agent.engine.learning.persona_context import load_goals
    content = load_goals()
    return {"exists": content is not None, "raw": content or ""}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _configure_workspace(workspace: str) -> None:
    """Configure all data directories to use a workspace-scoped layout.

    This must be called before any learning modules are instantiated.
    Layout::

        <workspace>/
            data/recipes/
            data/events/
            data/traces/
            data/in_flight/
            data/in_flight_archive/
            data/checkpoints/
            data/app_graphs/
            agent/          (community cache, tasks, directives — via NURTURE_WORKSPACE)
    """
    from pathlib import Path
    ws = Path(workspace).resolve()
    data = ws / "data"

    # Set NURTURE_WORKSPACE so TS-written files (community-recipes-cache.json,
    # directives.json) also land inside this workspace.
    os.environ["NURTURE_WORKSPACE"] = str(ws)

    # Configure Python learning modules
    from openclaw_agent.engine.learning import (
        recipe_store, event_log, trace_store, checkpoint_store,
    )
    from openclaw_agent.engine.learning.app_graph import set_app_graph_data_dir
    from openclaw_agent.engine.learning import graph_updater

    recipe_store._RECIPE_DIR = data / "recipes"
    event_log._DATA_DIR = data / "events"
    trace_store._DATA_DIR = data / "traces"
    trace_store._IN_FLIGHT_DIR = data / "in_flight"
    trace_store._IN_FLIGHT_ARCHIVE_DIR = data / "in_flight_archive"
    checkpoint_store._DATA_DIR = data / "checkpoints"
    set_app_graph_data_dir(str(data / "app_graphs"))
    graph_updater._PENDING_DIR = data / "pending_discoveries"

    # Ensure directories exist
    for d in [
        data / "recipes",
        data / "events",
        data / "traces",
        data / "in_flight",
        data / "in_flight_archive",
        data / "checkpoints",
        data / "app_graphs",
        data / "pending_discoveries",
    ]:
        d.mkdir(parents=True, exist_ok=True)

    logger.info(f"Workspace configured: {ws}")


# Module-level default device_id (set via --device-id, used as fallback)
_bound_device_id: str | None = None


def main():
    parser = argparse.ArgumentParser(description="OperationEngine HTTP + Gateway Node")
    parser.add_argument("--port", type=int, default=8600, help="HTTP 监听端口 (默认 8600)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="HTTP 监听地址 (默认 0.0.0.0)")
    parser.add_argument("--gateway-url", type=str, default="", help="Agent Gateway WebSocket URL")
    parser.add_argument("--gateway-token", type=str, default="", help="Gateway auth token")
    parser.add_argument("--node-id", type=str, default="", help="Node ID for gateway registration")
    parser.add_argument("--device-id", type=str, default="", help="Bound device serial (adb)")
    parser.add_argument("--workspace", type=str, default="", help="Workspace directory for isolated data")
    args = parser.parse_args()

    # CLI args override env vars for gateway config
    if args.gateway_url:
        os.environ["OPENCLAW_GATEWAY_URL"] = args.gateway_url
    if args.gateway_token:
        os.environ["OPENCLAW_GATEWAY_TOKEN"] = args.gateway_token
    if args.node_id:
        os.environ["OPENCLAW_NODE_ID"] = args.node_id

    # Workspace isolation: redirect all data paths
    if args.workspace:
        _configure_workspace(args.workspace)

    # Bind to a specific device
    global _bound_device_id
    if args.device_id:
        _bound_device_id = args.device_id
        logger.info(f"Bound to device: {_bound_device_id}")

    logger.info(f"HTTP Worker 启动在 {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
