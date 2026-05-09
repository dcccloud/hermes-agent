"""Asynchronous LLM-based recipe generator.

Reads unconsumed VLM traces from TraceStore, sends them to an LLM with
the humanize API documentation and the app page state graph, and writes
the resulting Python recipe to RecipeStore.

Designed to run as a background periodic task (see ``process_pending``).
"""

import json
import re
import textwrap
from typing import Any

from openai import OpenAI

from openclaw_agent.engine.common.config import get_config
from openclaw_agent.engine.common.logger import get_logger
from openclaw_agent.engine.learning.directive_context import (
    load_active_directives,
    build_directive_prompt_section,
)
from openclaw_agent.engine.learning.recipe_store import RecipeStore
from openclaw_agent.engine.learning.trace_store import TraceStore

logger = get_logger("recipe_generator")


def _get_page_graph_doc(operation: str) -> str:
    """Load the page graph documentation for the app that owns *operation*.

    Uses the merged AppGraph (seed + learned) so newly discovered
    pages and transitions are included in the prompt.
    """
    app = operation.split("/")[0] if "/" in operation else ""
    if not app:
        return ""
    try:
        from openclaw_agent.engine.learning.app_graph import get_app_graph
        return get_app_graph(app).build_page_graph_doc()
    except Exception:
        return ""

# Humanize API reference injected into the LLM prompt so it knows which
# helper functions are available for the generated recipe code.
_HUMANIZE_API_DOC = textwrap.dedent("""\
    ## Available humanize API (import from ``openclaw_agent.engine.learning.humanize``)

    ```python
    def tap_region(device, region: tuple[int,int,int,int],
                   hold_range=(0.03, 0.08)) -> None:
        \"\"\"Tap a random point inside region (x1,y1,x2,y2) with
        randomized press duration.\"\"\"

    def swipe_region(device,
                     start_region: tuple[int,int,int,int],
                     end_region: tuple[int,int,int,int],
                     duration_range=(0.3, 0.6),
                     use_bezier=True) -> None:
        \"\"\"Swipe from a random point in start_region to a random
        point in end_region using a Bezier curve.\"\"\"

    def long_press_region(device, region: tuple[int,int,int,int],
                          duration_range=(0.5, 1.0)) -> None:
        \"\"\"Long-press a random point inside region.\"\"\"

    def wait(base: float = 1.0, spread: float = 0.3) -> None:
        \"\"\"Humanized wait with triangular random distribution.\"\"\"

    def double_tap_region(device, region: tuple[int,int,int,int]) -> None:
        \"\"\"Double-tap a random point inside region.\"\"\"

    def back(device) -> None:
        \"\"\"Press the Android back button.\"\"\"
    ```

    The ``device`` parameter is a ``uiautomator2.Device`` instance
    (already passed in — **do NOT import uiautomator2**).

    ### Allowed ``device`` API (already available, no import needed):

    ```python
    # Element selector — returns a UiObject
    el = device(text="xxx")            # by text
    el = device(textContains="xxx")    # partial text match
    el = device(resourceId="xxx")      # by resource ID

    # UiObject methods:
    el.exists(timeout=2)       # -> bool, whether element is on screen
    el.click()                 # click the element center
    el.bounds()                # -> tuple (left, top, right, bottom)
    #   ⚠️  bounds() is a METHOD CALL — always use parentheses!
    #   ⚠️  bounds() returns a plain TUPLE, NOT an object with .left/.top!
    #   WRONG: el.bounds              (missing parentheses!)
    #   WRONG: el.bounds().left       (tuple has no .left!)
    #   RIGHT: tap_region(device, el.bounds())   # pass directly

    # Best practice — pass bounds() directly to tap_region:
    if el.exists(timeout=2):
        tap_region(device, el.bounds())

    # If you need individual coords, use index access:
    rect = el.bounds()  # (left, top, right, bottom)
    left, top, right, bottom = rect  # tuple unpacking

    # Device-level:
    device.window_size()       # -> (width, height)
    device.press("back")       # press back button
    ```
""")


def _get_op_state_checks(operation: str) -> dict[str, dict[str, str]]:
    """Load state_checks from YAML config for an operation.

    Returns dict with keys ``precondition`` and ``postcondition``,
    each mapping check names to expected values.
    Falls back to legacy ``precondition_feed_type`` /
    ``postcondition_feed_type`` fields if ``state_checks`` is absent.
    """
    import yaml
    from pathlib import Path

    result: dict[str, dict[str, str]] = {
        "precondition": {},
        "postcondition": {},
    }
    op_name = operation.split("/")[-1] if "/" in operation else operation
    ops_dir = (
        Path(__file__).resolve().parent.parent.parent
        / "config" / "operations"
    )
    for yf in ops_dir.glob("*.yaml"):
        try:
            data = yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
            for op in data.get("operations", []):
                if op.get("name") == op_name:
                    sc = op.get("state_checks") or {}
                    if sc:
                        result["precondition"] = sc.get(
                            "precondition") or {}
                        result["postcondition"] = sc.get(
                            "postcondition") or {}
                    else:
                        # Legacy fallback
                        pre_ft = op.get("precondition_feed_type")
                        post_ft = op.get("postcondition_feed_type")
                        if pre_ft:
                            result["precondition"]["feed_type"] = pre_ft
                        if post_ft:
                            result["postcondition"]["feed_type"] = post_ft
                    return result
        except Exception:
            pass
    return result


def _build_prompt(
    operation: str,
    step: str,
    traces: list[dict[str, Any]],
) -> str:
    """Build the LLM prompt for recipe generation.

    Injects the app page state graph so the LLM generates state-aware
    recipes that detect the current page before acting.
    """
    traces_json = json.dumps(traces, ensure_ascii=False, indent=2)
    page_graph = _get_page_graph_doc(operation)

    state_machine_section = ""
    if page_graph:
        state_machine_section = textwrap.dedent(f"""\
            {page_graph}

            ## 状态机 API（可以 import）

            ```python
            from openclaw_agent.engine.learning.humanize import detect_page, find_path

            # detect_page(device) -> str | None
            #   检测当前在哪个页面，返回页面 ID（如 "data_center"）
            #   未知页面返回 None

            # find_path(start, goal) -> list[str] | None
            #   返回从 start 到 goal 的最短路径（页面 ID 列表）
            ```

        """)

    # Load operation state checks for pre/post condition hints
    state_checks = _get_op_state_checks(operation)
    constraint_section = ""
    pre_checks = state_checks.get("precondition", {})
    post_checks = state_checks.get("postcondition", {})
    if pre_checks or post_checks:
        lines = [
            "## 操作状态约束（框架层自动检查，recipe 也应遵守）\n"]
        if pre_checks:
            lines.append("### 准入条件 (precondition)")
            for key, val in pre_checks.items():
                lines.append(f"- {key} == {val}")
            lines.append(
                "\nexecute() 开头应检查准入条件，不满足时 return False。\n")
        if post_checks:
            lines.append("### 结束确认 (postcondition)")
            for key, val in post_checks.items():
                lines.append(f"- {key} == {val}")
            lines.append(
                "\nverify() 应验证所有结束条件。\n")
        constraint_section = "\n".join(lines) + "\n"

    # Load community directives for the app
    app_name = operation.split("/")[0] if "/" in operation else ""
    directives = load_active_directives(app_name) if app_name else []
    directive_section = build_directive_prompt_section(directives)

    return textwrap.dedent(f"""\
        你是一个 Android 自动化 RPA 工程师。

        以下是操作 **{operation}** 的步骤 **{step}** 的执行记录
        （可能有多次执行）。每条记录包含：
        - ``rpa_commands_before_fallback``：之前的 RPA recipe 在失败前
          执行的 u2/ADB 命令（说明 recipe 做到了哪一步、在哪里失败了）。
        - ``actions``：VLM 兜底后成功完成任务的步骤轨迹
          （VLM 从 recipe 失败的地方接手并完成了任务）。

        ```json
        {traces_json}
        ```

        {state_machine_section}

        {constraint_section}

        {directive_section}

        请分析 **完整流程**（recipe 做的部分 + VLM 补的部分），
        生成一个**状态感知**的 Python RPA 函数。

        ## 核心原则：状态机思维

        recipe 是一个从**当前页面状态**到**目标页面状态**的状态转换器：
        1. **先检测** — 用 ``detect_page(device)`` 确认当前在哪个页面
        2. **跳过已达成** — 如果已经在目标页面，直接返回 True
        3. **就近导航** — 如果在已知的其他页面，根据状态图找最短路径导航
        4. **兜底回退** — 如果在未知页面，先 ``back()`` 尝试回到已知页面
        5. **每步验证** — 每个导航动作后都用 ``detect_page`` 确认到达

        状态图中每个页面都有 **描述（description）** 和 **到达路径（discovery_path）**。
        利用这些信息理解页面的功能，生成更精准的导航和验证逻辑。

        ## 坐标和元素区域

        VLM 的 actions 中每个 Tap 动作包含：
        - ``element``: [x, y] — VLM 点击的中心坐标（**相对坐标 0-999**）
        - ``element_region``: [left, top, right, bottom] — 被点击元素的实际边界矩形
          （**绝对像素坐标**，来自 UI hierarchy 的精确元素 bounds）
        - ``screen_width``, ``screen_height``: 屏幕实际像素尺寸

        **坐标转换公式**（仅用于 ``element``）：绝对像素 = 相对坐标 / 1000 × 屏幕像素尺寸
        ``element_region`` 已经是绝对像素，可直接用于 ``tap_region``。

        **关键**：当 u2 元素选择器（如 ``device(text="xxx")``）找不到元素时，
        应**直接**使用 ``element_region`` 作为 ``tap_region`` 的坐标。
        ``element_region`` 来自 UI 元素树的精确 bounds，
        比从单点坐标 ±30px 估算精确得多。

        {_HUMANIZE_API_DOC}

        ## 输出要求

        1. 输出完整的 Python 文件内容，包含 import 和 ``execute(device) -> bool``。
        2. 函数签名必须是 ``def execute(device) -> bool:``。
        3. **函数开头必须调用 ``detect_page(device)``** 检测当前页面。
           如果已在目标状态，直接 ``return True``。
        4. **必须使用 humanize API**（``tap_region``、``swipe_region``、
           ``wait``、``back``、``detect_page``、``find_path`` 等），
           **不要**直接调用 ``device.click(x, y)`` 或 ``device.swipe(...)``。
        5. **坐标 fallback 策略**：优先用 u2 元素选择器定位；选择器失败时，
           用 VLM trace 中的 ``element_region``（已转换为绝对像素）
           作为 ``tap_region`` 的精确区域，而非从单点坐标猜测。
        6. 操作之间加 ``wait()`` 模拟人类节奏。
        7. **优先使用 u2 元素选择器** ``device(text="xxx")`` 来定位和点击，
           坐标区域仅作为元素选择器的后备。
        8. 在关键操作后用 ``detect_page(device)`` 或
           ``device(textContains="...").exists(timeout=N)`` 做验证。
        9. **只允许 import ``openclaw_agent.engine.learning.humanize``**。
           严禁 ``import uiautomator2``、``import time`` 等任何其他模块。
           ``device`` 对象已作为参数传入，可直接调用 ``device(text=...)``
           等方法，无需 import。
        10. 代码中加简洁注释。
        11. **必须同时生成 ``def verify(device) -> bool:`` 独立验证函数**。
           verify 函数用于在 execute 执行完毕后独立验证目标是否达成：
           - 导航类操作（到达某页面）：用 ``detect_page(device) == "目标页面ID"`` 验证
           - 操作类任务（如查看评论）：用具体 UI 元素检查验证结果
           - verify 必须独立于 execute，不依赖 execute 的中间状态
           - verify 只做检查，不执行任何导航/点击操作
        12. **文件顶部必须包含元数据注释**（在 import 之前），格式如下：
           ``# recipe_meta: target_page=目标页面ID, verify_type=page|custom, description=简短中文描述``
           - target_page: 如果是导航到某页面的操作，填页面 ID；否则留空
           - verify_type: ``page``（用 detect_page 验证）或 ``custom``（自定义验证逻辑）
           - description: 简短描述这个 recipe 做什么
        13. **前置状态检查**：如果操作有准入条件（见上方「操作状态约束」），
           execute() 开头必须检查所有准入条件。
           - feed_type: 用 u2 元素检测验证页面类型（如检查评论按钮确认是视频页）
           - like_status: 检测点赞图标颜色判断是否已点赞
           - follow_status: 检测关注按钮判断是否已关注
           不满足时 return False。
        14. **后置状态检查**：如果操作有结束确认条件，
           verify() 函数应验证所有结束条件（页面状态、业务状态如点赞/关注是否生效）。
        15. 只输出 Python 代码，用 ```python ... ``` 包裹，不要其他文字。
    """)


def _extract_code(response: str) -> str:
    """Extract Python code block from LLM response."""
    m = re.search(r"```python\s*\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1).strip()
    return response.strip()


def _extract_recipe_meta(code: str) -> dict[str, Any]:
    """Parse ``# recipe_meta: key=val, key=val`` from code header.

    Returns a dict suitable for merging into ``.meta.json``.
    """
    meta: dict[str, Any] = {}
    m = re.search(
        r"#\s*recipe_meta:\s*(.+)", code)
    if not m:
        return meta
    raw = m.group(1).strip()
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key == "target_page":
            meta["target_page"] = val if val else None
        elif key == "verify_type":
            meta["verify_type"] = val if val in ("page", "custom", "vlm") else None
        elif key == "description":
            meta["description"] = val
    return meta


def _validate_code(code: str) -> tuple[bool, str]:
    """Static + dynamic checks on generated recipe code.

    Returns ``(True, "")`` when valid, or ``(False, reason)`` with a
    human-readable error message that can be fed back to the LLM for retry.
    """
    if "def execute(" not in code:
        return False, "Missing `def execute(device) -> bool:` function definition"
    if "-> bool" not in code:
        return False, "Missing `-> bool` return type annotation on execute()"
    for forbidden in ["device.click(", "device.swipe(", "device.long_click("]:
        if forbidden in code:
            msg = (
                f"Forbidden raw device call `{forbidden}` — "
                "use humanize API (tap_region / swipe_region) instead"
            )
            logger.warning(msg)
            return False, msg
    # Only allow imports from our own modules
    import re as _re
    for m in _re.finditer(r"^\s*(?:from|import)\s+(\S+)", code, _re.MULTILINE):
        mod = m.group(1)
        if not mod.startswith("openclaw_agent.engine.learning."):
            msg = (
                f"Forbidden import `{mod}` — only "
                "`openclaw_agent.engine.learning.humanize` is allowed"
            )
            logger.warning(msg)
            return False, msg
    try:
        compile(code, "<recipe>", "exec")
    except SyntaxError as e:
        msg = f"SyntaxError: {e}"
        logger.warning(msg)
        return False, msg
    # Static checks for common u2 API misuse
    if _re.search(r"\.bounds\s*(?!\s*\()", code):
        msg = (
            "`.bounds` used without `()` — bounds() is a METHOD CALL. "
            "WRONG: el.bounds.left. "
            "RIGHT: tap_region(device, el.bounds())"
        )
        logger.warning(msg)
        return False, msg
    if _re.search(r"\.bounds\(\)\s*\.\s*(left|top|right|bottom)", code):
        msg = (
            "`.bounds().left/top/right/bottom` — bounds() returns a plain "
            "tuple, NOT an object with named attributes. "
            "RIGHT: tap_region(device, el.bounds()) or use tuple unpacking"
        )
        logger.warning(msg)
        return False, msg
    # Warn if verify() is missing (non-blocking)
    if "def verify(" not in code:
        logger.info("Recipe has no verify() function — will trust execute() return value")
    # Dry-run with mock device to catch runtime errors
    ok, err = _mock_dry_run(code)
    if not ok:
        msg = f"Dry-run execution failed: {err}"
        logger.warning(msg)
        return False, msg
    return True, ""


# ---------------------------------------------------------------------------
# Mock dry-run: execute recipe against a fake device to catch runtime errors
# ---------------------------------------------------------------------------

class _MockUiObject:
    """Simulates ``uiautomator2.UiObject`` for dry-run.

    ``bounds()`` returns a plain tuple because some u2 versions / the
    DeviceRecorder wrapper strip the namedtuple.  Recipes must handle
    this (use index access or pass the tuple directly).
    """

    def exists(self, timeout: float = 0) -> bool:
        return True

    def bounds(self) -> tuple[int, int, int, int]:
        return (100, 200, 300, 400)

    def click(self, *a, **kw) -> None:
        pass

    def get_text(self) -> str:
        return ""

    def set_text(self, text: str) -> None:
        pass

    def info(self) -> dict:
        return {"text": "", "bounds": {"left": 100, "top": 200, "right": 300, "bottom": 400}}

    def __bool__(self) -> bool:
        return True

    def __call__(self, **kw) -> "_MockUiObject":
        return self


class _MockDevice:
    """Simulates ``uiautomator2.Device`` for dry-run validation."""

    def __call__(self, **kw) -> _MockUiObject:
        return _MockUiObject()

    def window_size(self) -> tuple[int, int]:
        return (1080, 2400)

    def dump_hierarchy(self) -> str:
        return "<hierarchy></hierarchy>"

    def press(self, key: str) -> None:
        pass

    def click(self, x: int, y: int) -> None:
        pass

    def swipe(self, *a, **kw) -> None:
        pass


def _mock_dry_run(code: str) -> tuple[bool, str]:
    """Execute recipe code with a mock device to catch runtime errors.

    Returns (True, "") on success or (False, error_message) on failure.
    Patches humanize functions to no-ops so only the recipe's own logic
    is tested for correctness.
    """
    import types

    mock_device = _MockDevice()

    # Build a fake humanize module so recipe imports resolve
    fake_humanize = types.ModuleType("openclaw_agent.engine.learning.humanize")
    fake_humanize.tap_region = lambda device, region, **kw: None  # type: ignore[attr-defined]
    fake_humanize.swipe_region = lambda device, *a, **kw: None  # type: ignore[attr-defined]
    fake_humanize.long_press_region = lambda device, region, **kw: None  # type: ignore[attr-defined]
    fake_humanize.wait = lambda *a, **kw: None  # type: ignore[attr-defined]
    fake_humanize.double_tap_region = lambda device, region, **kw: None  # type: ignore[attr-defined]
    fake_humanize.back = lambda device: None  # type: ignore[attr-defined]
    # detect_page: cycle through typical page states so recipes exercise
    # multiple navigation branches (home → profile → creator_center → …)
    _PAGE_CYCLE = [
        None, "app_home", "app_home", "profile",
        "creator_center", "data_center", "data_center",
    ]
    _detect_idx: list[int] = [0]

    def _mock_detect_page(device: Any, app: str = "douyin") -> str | None:
        idx = _detect_idx[0]
        _detect_idx[0] += 1
        return _PAGE_CYCLE[idx % len(_PAGE_CYCLE)]

    fake_humanize.detect_page = _mock_detect_page  # type: ignore[attr-defined]

    def _mock_find_path(start: str, goal: str, **kw: Any) -> list[str]:
        # Return a multi-step path to exercise intermediate navigation code
        _ALL_PAGES = ["app_home", "profile", "creator_center", "data_center"]
        try:
            si = _ALL_PAGES.index(start)
            gi = _ALL_PAGES.index(goal)
        except ValueError:
            return [start, goal]
        if si <= gi:
            return _ALL_PAGES[si : gi + 1]
        return [start, goal]

    fake_humanize.find_path = _mock_find_path  # type: ignore[attr-defined]

    import sys
    saved_modules: dict[str, types.ModuleType | None] = {}
    mod_keys = [
        "openclaw_agent.engine.learning.humanize",
        "openclaw_agent.engine.learning",
        "openclaw_agent.engine",
    ]
    try:
        for k in mod_keys:
            saved_modules[k] = sys.modules.get(k)
        sys.modules["openclaw_agent.engine.learning.humanize"] = fake_humanize

        namespace: dict[str, Any] = {}
        exec(compile(code, "<recipe-dryrun>", "exec"), namespace)

        execute_fn = namespace.get("execute")
        if execute_fn is None:
            return False, "no execute() function found"

        execute_fn(mock_device)

        # Also dry-run verify() if present
        verify_fn = namespace.get("verify")
        if verify_fn is not None:
            verify_fn(mock_device)

        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        for k in mod_keys:
            prev = saved_modules.get(k)
            if prev is not None:
                sys.modules[k] = prev
            else:
                sys.modules.pop(k, None)


class RecipeGenerator:
    """Consumes VLM traces and produces Python recipes via LLM."""

    def __init__(
        self,
        trace_store: TraceStore | None = None,
        recipe_store: RecipeStore | None = None,
    ):
        self.trace_store = trace_store or TraceStore()
        self.recipe_store = recipe_store or RecipeStore()

    def process_pending(self) -> int:
        """Scan all unconsumed traces and generate recipes.

        Groups traces by ``(operation, step)`` and produces one recipe
        per group, feeding all available traces for cross-validation.

        Returns the number of recipes generated.
        """
        pending = self.trace_store.get_pending(limit=100)
        if not pending:
            logger.info("No pending traces to process")
            return 0

        # Group by (operation, step)
        groups: dict[tuple[str, str], list[dict]] = {}
        for t in pending:
            key = (t["operation"], t["step"])
            groups.setdefault(key, []).append(t)

        generated = 0
        for (op, step), traces in groups.items():
            try:
                code = self._generate_recipe(traces, op, step)
                if code:
                    extra = _extract_recipe_meta(code)
                    self.recipe_store.save(op, step, code, extra_meta=extra)
                    consumed_ids = {t["id"] for t in traces}
                    self.trace_store.mark_consumed(consumed_ids)
                    generated += 1
                    logger.info(f"Generated recipe for {op}/{step} from {len(traces)} traces")
                    try:
                        from openclaw_agent.engine.learning.event_log import emit
                        emit("recipe.generated", {
                            "from_traces_count": len(traces),
                            "source": "llm",
                        }, operation=op, step=step)
                    except Exception:
                        pass
            except Exception:
                logger.exception(f"Failed to generate recipe for {op}/{step}")

        return generated

    def process_for_step(self, operation: str, step: str) -> int:
        """Generate a recipe for a specific (operation, step) pair.

        Checks community recipes first — if a high-quality community
        recipe exists, adopts it directly instead of calling the LLM.
        Only processes unconsumed traces matching the given step.
        Returns 1 if a recipe was generated, 0 otherwise.
        """
        # Check community recipe cache before LLM generation
        app = operation.split("/")[0] if "/" in operation else ""
        if app:
            try:
                from openclaw_agent.engine.learning.community_recipe import (
                    get_community_best_recipe,
                    maybe_adopt_community_recipe,
                )
                community = get_community_best_recipe(app, operation, step)
                if community and maybe_adopt_community_recipe(
                    self.recipe_store, operation, step, community,
                ):
                    logger.info(
                        f"Adopted community recipe for {operation}/{step}")
                    return 1
            except Exception:
                logger.debug(
                    f"Community recipe check failed for {operation}/{step}",
                    exc_info=True)

        pending = self.trace_store.get_pending(
            operation=operation, limit=50)
        traces = [t for t in pending if t.get("step") == step]
        if not traces:
            logger.debug(f"No pending traces for {operation}/{step}")
            return 0
        try:
            code = self._generate_recipe(traces, operation, step)
            if code:
                extra = _extract_recipe_meta(code)
                self.recipe_store.save(
                    operation, step, code, extra_meta=extra)
                consumed_ids = {t["id"] for t in traces}
                self.trace_store.mark_consumed(consumed_ids)
                logger.info(
                    f"Generated recipe for {operation}/{step} "
                    f"from {len(traces)} traces")
                return 1
        except Exception:
            logger.exception(
                f"Failed to generate recipe for {operation}/{step}")
        return 0

    _MAX_FIX_RETRIES = 2

    def _generate_recipe(
        self,
        traces: list[dict[str, Any]],
        operation: str,
        step: str,
    ) -> str | None:
        """Call LLM to produce recipe code from traces.

        If the first attempt fails validation, feeds the error back to
        the LLM for up to ``_MAX_FIX_RETRIES`` self-correction rounds.
        """
        config = get_config()

        # Prefer dedicated recipe_gen config; fall back to vision_read
        rg = config.recipe_gen
        if rg.api_key and rg.model_name:
            api_key = rg.api_key
            base_url = rg.base_url
            model = rg.model_name
        else:
            api_key = config.vision_read.api_key
            base_url = config.vision_read.base_url
            model = config.vision_read.model_name

        if not api_key:
            logger.error("No API key configured for recipe generation")
            return None

        trace_actions = []
        for t in traces:
            entry: dict[str, Any] = {
                "timestamp": t.get("timestamp"),
                "device_id": t.get("device_id"),
                "actions": t.get("actions", []),
            }
            extra = t.get("extra") or {}
            rpa_cmds = extra.get("rpa_commands_before_fallback")
            if rpa_cmds:
                entry["rpa_commands_before_fallback"] = rpa_cmds
            trace_actions.append(entry)

        prompt = _build_prompt(operation, step, trace_actions)
        client = OpenAI(api_key=api_key, base_url=base_url)

        messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]

        for attempt in range(1 + self._MAX_FIX_RETRIES):
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=3000,
            )

            raw = (resp.choices[0].message.content or "").strip()
            code = _extract_code(raw)
            valid, reason = _validate_code(code)

            if valid:
                if attempt > 0:
                    logger.info(
                        f"Recipe for {operation}/{step} passed validation "
                        f"after {attempt} fix attempt(s)"
                    )
                return code

            logger.warning(
                f"Recipe validation failed for {operation}/{step} "
                f"(attempt {attempt + 1}/{1 + self._MAX_FIX_RETRIES}): {reason}"
            )

            if attempt < self._MAX_FIX_RETRIES:
                # Feed the error back so LLM can self-correct
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        f"上面的代码有错误，请修正后重新输出完整代码。\n\n"
                        f"错误信息：{reason}\n\n"
                        f"问题代码：\n```python\n{code}\n```\n\n"
                        f"请只输出修正后的完整 Python 代码，"
                        f"用 ```python ... ``` 包裹。"
                    ),
                })

        logger.error(
            f"Recipe generation for {operation}/{step} failed after "
            f"{1 + self._MAX_FIX_RETRIES} attempts"
        )
        return None
