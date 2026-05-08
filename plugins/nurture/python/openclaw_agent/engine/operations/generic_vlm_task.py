"""万能 VLM Operation — 纯 VLM 执行任意任务

No seed RPA (rpa_fn always returns False).  The first successful VLM
execution saves a trace; RecipeGenerator then produces a recipe so
subsequent runs are faster and cheaper.

This enables the system to learn *entirely new* operations from scratch:
  server sends task_prompt → VLM figures it out → trace → recipe → done.

Required params:
    task_prompt (str): What the VLM should accomplish.

Optional params:
    task_name (str): Stable key for recipe storage (default: "unnamed").
        Use a descriptive, slug-style name like "check_notification_bell"
        so the generated recipe is stored under a meaningful path.
    app (str): App namespace for recipe path (default: "generic").
    verify_prompt (str): If provided, the VLM is asked this question
        after the main task; "yes" in the response means success.

Reserved (injected by server.py from yaml metadata, not for direct caller use):
    _target_app (str): yaml `affiliated_app` (e.g. "facebook"). Used as
        the app namespace for recipe storage when set, overriding the
        caller's `app` param. Also fed into the VLM prompt as context.
    _target_app_package (str): yaml `app_package` (e.g.
        "com.facebook.katana"). When set, the operation:
          - ensures the package is foreground BEFORE handing off to VLM
          - hard-prefixes the VLM prompt forbidding app switches
          - verifies foreground at the end and marks failed if drifted
"""

import time

from openclaw_agent.engine.common.logger import get_logger
from openclaw_agent.engine.core.operation import OperationResult, ExecutionContext, Operation
from openclaw_agent.engine.learning.adaptive_step import AdaptiveStep
from openclaw_agent.engine.utils.interaction import human_sleep

logger = get_logger("generic_vlm_task")


def _current_package(device) -> str:
    try:
        cur = device.app_current() or {}
        pkg = cur.get("package", "")
        return pkg if isinstance(pkg, str) else ""
    except Exception as e:
        logger.warning(f"app_current() failed: {e}")
        return ""


def _ensure_foreground(device, app_package: str, app_label: str) -> bool:
    """Make sure `app_package` is foreground; cold-start if not.
    Returns True if foreground == target after the attempt.
    """
    cur = _current_package(device)
    if cur == app_package:
        return True
    logger.info(
        f"[{app_label}] foreground is {cur!r}, expected {app_package!r}; launching")
    try:
        device.app_start(app_package, wait=True)
    except Exception as e:
        logger.error(f"[{app_label}] app_start({app_package}) failed: {e}")
        return False
    human_sleep(3, 0.5)
    cur = _current_package(device)
    return cur == app_package


def _build_constrained_prompt(
    user_prompt: str, target_app: str, target_app_package: str
) -> str:
    return (
        f"重要约束（必须遵守）：本次任务必须在 {target_app}（包名 "
        f"{target_app_package}）应用内完成。禁止切换到任何其他 app。"
        f"如果发现自己已经离开该 app（看到桌面、其他 app 的 UI、通知中心等），"
        f"立即按 Home 键再点击 {target_app} 图标重新进入，不要在错的 app 里继续动作。\n\n"
        f"用户任务：{user_prompt}"
    )


class GenericVlmTaskOperation(Operation):
    """Pure-VLM operation that can learn any task from scratch."""

    REQUIRED_PARAMS = ["task_prompt"]
    MAX_STEPS = 20

    def execute(self, context: ExecutionContext) -> OperationResult:
        device = context.device
        agent = context.agent
        params = context.params

        task_prompt = params["task_prompt"]
        task_name = params.get("task_name", "unnamed")

        # Target app context injected by server.py from yaml metadata.
        target_app = params.get("_target_app", "")
        target_app_package = params.get("_target_app_package", "")

        # `app` is used as the recipe storage namespace. Prefer the yaml-
        # declared affiliated_app over the caller-provided `app` param so
        # recipes always land under a stable namespace (e.g. facebook/...
        # not generic/...).
        app = target_app or params.get("app", "generic")
        label = f"{app}/{task_name}"

        # ── Layer 1: ENTRY GUARD ───────────────────────────────────────
        if target_app_package:
            ok = _ensure_foreground(device, target_app_package, label)
            if not ok:
                cur = _current_package(device)
                return self.failed(
                    f"无法将 {target_app_package} 切到前台 (current={cur!r})"
                )

        # Detect starting page so _update_graph can record transition
        # edges (prior_page → discovered_page) and discovery_path.
        nav_path: list[str] = []
        try:
            from openclaw_agent.engine.learning.app_graph import get_app_graph
            graph = get_app_graph(app)
            start_page = graph.detect_page(device)
            if not start_page:
                # App may still be loading (splash screen); retry once
                time.sleep(1.5)
                start_page = graph.detect_page(device)
            if start_page:
                nav_path.append(start_page)
                logger.info(f"[{label}] detected start page: {start_page}")
            else:
                logger.debug(f"[{label}] no start page detected after retry")
        except Exception:
            logger.debug(f"[{label}] start page detection failed")

        # ── Layer 2: PROMPT INJECTION ─────────────────────────────────
        vlm_prompt = task_prompt
        if target_app and target_app_package:
            vlm_prompt = _build_constrained_prompt(
                task_prompt, target_app, target_app_package
            )

        step = AdaptiveStep(label, "main")
        ok = step.run(
            device, agent,
            rpa_fn=lambda: False,
            vlm_prompt=vlm_prompt,
            nav_path=nav_path,
        )

        if not ok:
            return self.failed(f"VLM 任务失败: {task_name}")

        # ── Layer 3: EXIT VERIFICATION ────────────────────────────────
        if target_app_package:
            cur = _current_package(device)
            if cur != target_app_package:
                logger.warning(
                    f"[{label}] VLM ended outside {target_app_package} "
                    f"(current={cur!r}) — task likely drifted to wrong app"
                )
                return self.failed(
                    f"任务结束时已不在 {target_app}（当前: {cur!r}）；"
                    f"VLM 可能被引导到了错误的 app 上"
                )

        return self.success(data={
            "message": f"任务 '{task_name}' 完成",
            "task_name": task_name,
            "app": app,
        })
