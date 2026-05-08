"""LLM-driven recipe quality reviewer.

After each recipe execution, the reviewer examines runtime metrics
(success rate, duration, VLM fallback frequency) and decides whether
the recipe needs optimization.  When it does, it generates a candidate
recipe that will be alternated with the current one for evaluation.

Replaces the old "trigger recipe gen on every VLM fallback" approach
with a metrics-aware, LLM-judged optimization loop.
"""

import json
import textwrap
from typing import Any

from openai import OpenAI

from openclaw_agent.engine.common.config import get_config
from openclaw_agent.engine.common.logger import get_logger
from openclaw_agent.engine.learning.recipe_generator import (
    _extract_code,
    _extract_recipe_meta,
    _validate_code,
    _build_prompt,
    _get_op_state_checks,
    _HUMANIZE_API_DOC,
)
from openclaw_agent.engine.learning.recipe_store import RecipeStore
from openclaw_agent.engine.learning.trace_store import TraceStore

logger = get_logger("recipe_reviewer")

# Only consider optimization after this many total runs
_MIN_RUNS_BEFORE_REVIEW = 3
# Thresholds that indicate the recipe is "good enough" — skip review
_GOOD_SUCCESS_RATE = 0.9
_GOOD_MAX_VLM_RATE = 0.1
_MAX_FIX_RETRIES = 2


class RecipeReviewer:
    """Reviews recipe metrics and optionally generates improved candidates."""

    def __init__(
        self,
        recipe_store: RecipeStore | None = None,
        trace_store: TraceStore | None = None,
    ):
        self.recipe_store = recipe_store or RecipeStore()
        self.trace_store = trace_store or TraceStore()

    def should_review(self, operation: str, step: str) -> bool:
        """Cheap pre-check: does this recipe warrant an LLM review?

        Returns False when the recipe is already performing well or
        has too few runs.  Avoids wasting LLM calls on healthy recipes.
        """
        meta = self.recipe_store.read_meta(operation, step)
        total = meta.get("total_runs", 0)
        if total < _MIN_RUNS_BEFORE_REVIEW:
            return False

        # Already has a pending candidate — don't create another
        if self.recipe_store.has_candidate(operation, step):
            return False

        success = meta.get("success", 0)
        success_rate = success / total if total else 0
        vlm_count = meta.get("vlm_fallback_count", 0)
        vlm_rate = vlm_count / total if total else 0

        # Recipe is performing well — no review needed
        if success_rate >= _GOOD_SUCCESS_RATE and vlm_rate <= _GOOD_MAX_VLM_RATE:
            logger.debug(
                f"[{operation}/{step}] skip review: "
                f"success={success_rate:.0%} vlm={vlm_rate:.0%}")
            return False

        return True

    def review_and_maybe_improve(
        self, operation: str, step: str,
    ) -> dict[str, Any]:
        """Run LLM review on the recipe's metrics and traces.

        Returns a structured review dict with keys:
        - ``verdict``: "good" | "needs_improvement"
        - ``issues``: list of identified problems
        - ``candidate_generated``: bool
        """
        meta = self.recipe_store.read_meta(operation, step)
        current_py = self.recipe_store._recipe_path(operation, step)
        current_code = ""
        if current_py.exists():
            current_code = current_py.read_text(encoding="utf-8")

        # Gather recent traces (both consumed and pending)
        recent_traces = self.trace_store.get_pending(
            operation=operation, limit=10)
        recent_traces = [t for t in recent_traces if t.get("step") == step]

        review = self._call_review_llm(
            operation, step, meta, current_code, recent_traces)

        if review.get("verdict") == "needs_improvement":
            ok = self._generate_candidate(
                operation, step, meta, current_code,
                review.get("issues", []), recent_traces)
            review["candidate_generated"] = ok
        else:
            review["candidate_generated"] = False

        return review

    def _call_review_llm(
        self,
        operation: str,
        step: str,
        meta: dict[str, Any],
        current_code: str,
        traces: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Ask LLM to review recipe metrics and current code."""
        config = get_config()
        rg = config.recipe_gen
        if rg.api_key and rg.model_name:
            api_key, base_url, model = rg.api_key, rg.base_url, rg.model_name
        else:
            api_key = config.vision_read.api_key
            base_url = config.vision_read.base_url
            model = config.vision_read.model_name

        if not api_key:
            logger.error("No API key for recipe review")
            return {"verdict": "good", "issues": []}

        metrics_summary = json.dumps({
            "total_runs": meta.get("total_runs", 0),
            "success": meta.get("success", 0),
            "failure": meta.get("failure", 0),
            "streak": meta.get("streak", 0),
            "avg_duration_ms": meta.get("avg_duration_ms"),
            "last_duration_ms": meta.get("last_duration_ms"),
            "vlm_fallback_count": meta.get("vlm_fallback_count", 0),
        }, ensure_ascii=False, indent=2)

        trace_summary = ""
        if traces:
            trace_entries = []
            for t in traces[-3:]:
                trace_entries.append({
                    "timestamp": t.get("timestamp"),
                    "actions": t.get("actions", [])[:5],
                    "rpa_before": (t.get("extra") or {}).get(
                        "rpa_commands_before_fallback", [])[:5],
                })
            trace_summary = json.dumps(
                trace_entries, ensure_ascii=False, indent=2)

        # Build state_checks context for the reviewer
        sc = _get_op_state_checks(operation)
        sc_pre = sc.get("precondition", {})
        sc_post = sc.get("postcondition", {})
        state_checks_section = ""
        if sc_pre or sc_post:
            sc_lines = ["## 操作状态约束"]
            if sc_pre:
                sc_lines.append("### 准入条件 (precondition)")
                for k, v in sc_pre.items():
                    sc_lines.append(f"- {k} == {v}")
            if sc_post:
                sc_lines.append("### 结束确认 (postcondition)")
                for k, v in sc_post.items():
                    sc_lines.append(f"- {k} == {v}")
            state_checks_section = "\n".join(sc_lines)

        prompt = textwrap.dedent(f"""\
            你是 RPA recipe 质量评审员。分析以下 recipe 的运行指标和代码，
            判断是否需要优化。

            ## 操作: {operation} / 步骤: {step}

            ## 运行指标
            ```json
            {metrics_summary}
            ```

            ## 当前 Recipe 代码
            ```python
            {current_code if current_code else "(无 learned recipe，使用 seed RPA)"}
            ```

            {"## 最近 VLM 兜底 trace" + chr(10) + "```json" + chr(10) + trace_summary + chr(10) + "```" if trace_summary else ""}

            {state_checks_section}

            ## 评审标准
            1. 成功率 < 90% → 需要优化
            2. VLM 兜底率 > 10% → recipe 不够可靠，需要优化
            3. 平均耗时过长（与操作复杂度不匹配）→ 可以优化
            4. 连续成功 streak 被打断 → 可能有间歇性问题
            5. recipe 缺少准入条件检查（如操作声明了 like_status 但 recipe 未检查）→ 需要优化
            6. recipe 缺少结束确认（如操作声明了 postcondition 但 verify() 未验证）→ 需要优化

            ## 输出格式（严格 JSON）
            ```json
            {{
              "verdict": "good" | "needs_improvement",
              "issues": ["问题描述1", "问题描述2"],
              "suggestions": ["优化建议1", "优化建议2"]
            }}
            ```

            只输出 JSON，不要其他文字。
        """)

        client = OpenAI(api_key=api_key, base_url=base_url)
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=1000,
            )
            raw = (resp.choices[0].message.content or "").strip()
            # Extract JSON from possible markdown wrapper
            import re
            m = re.search(r"```json\s*\n(.*?)```", raw, re.DOTALL)
            json_str = m.group(1).strip() if m else raw
            return json.loads(json_str)
        except Exception:
            logger.exception(f"LLM review failed for {operation}/{step}")
            return {"verdict": "good", "issues": []}

    def _generate_candidate(
        self,
        operation: str,
        step: str,
        meta: dict[str, Any],
        current_code: str,
        issues: list[str],
        traces: list[dict[str, Any]],
    ) -> bool:
        """Generate an improved candidate recipe based on review findings.

        Checks community recipes first — if a community recipe is
        better than the current local one, adopts it directly.
        """
        # Check community recipe before LLM generation
        app = operation.split("/")[0] if "/" in operation else ""
        if app:
            try:
                from openclaw_agent.engine.learning.community_recipe import (
                    get_community_best_recipe,
                    maybe_adopt_community_recipe,
                )
                total = meta.get("total_runs", 0)
                success = meta.get("success", 0)
                local_rate = success / total if total else None
                community = get_community_best_recipe(app, operation, step)
                if community and maybe_adopt_community_recipe(
                    self.recipe_store, operation, step, community,
                    local_success_rate=local_rate,
                    local_sample_count=total,
                ):
                    logger.info(
                        f"Adopted community recipe for {operation}/{step} "
                        f"(replacing local rate={local_rate})")
                    return True
            except Exception:
                logger.debug(
                    f"Community recipe check failed for {operation}/{step}",
                    exc_info=True)

        config = get_config()
        rg = config.recipe_gen
        if rg.api_key and rg.model_name:
            api_key, base_url, model = rg.api_key, rg.base_url, rg.model_name
        else:
            api_key = config.vision_read.api_key
            base_url = config.vision_read.base_url
            model = config.vision_read.model_name

        if not api_key:
            return False

        issues_text = "\n".join(f"- {i}" for i in issues)
        trace_actions = []
        for t in traces:
            entry: dict[str, Any] = {
                "timestamp": t.get("timestamp"),
                "actions": t.get("actions", []),
            }
            extra = t.get("extra") or {}
            rpa_cmds = extra.get("rpa_commands_before_fallback")
            if rpa_cmds:
                entry["rpa_commands_before_fallback"] = rpa_cmds
            trace_actions.append(entry)

        # Build on the standard recipe generation prompt, then add
        # the current code and review feedback as extra context
        base_prompt = _build_prompt(operation, step, trace_actions)
        improvement_prompt = textwrap.dedent(f"""\
            {base_prompt}

            ## 当前 Recipe（需要优化）
            ```python
            {current_code if current_code else "(无)"}
            ```

            ## 评审发现的问题
            {issues_text}

            ## 额外要求
            - 基于当前代码进行改进，修复上述问题
            - 保持代码结构不变，只做必要修改
            - 如果当前代码为空，根据 trace 生成全新 recipe
            - 必须包含独立的 ``def verify(device) -> bool:`` 验证函数
            - 必须包含 ``# recipe_meta: target_page=..., verify_type=..., description=...`` 元数据注释
        """)

        client = OpenAI(api_key=api_key, base_url=base_url)
        messages: list[dict[str, str]] = [
            {"role": "user", "content": improvement_prompt}
        ]

        for attempt in range(1 + _MAX_FIX_RETRIES):
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.2,
                    max_tokens=3000,
                )
                raw = (resp.choices[0].message.content or "").strip()
                code = _extract_code(raw)
                valid, reason = _validate_code(code)

                if valid:
                    extra = _extract_recipe_meta(code)
                    self.recipe_store.save_candidate(
                        operation, step, code,
                        reason="; ".join(issues),
                        extra_meta=extra)
                    logger.info(
                        f"Candidate recipe generated for {operation}/{step}")
                    return True

                logger.warning(
                    f"Candidate validation failed ({attempt + 1}/"
                    f"{1 + _MAX_FIX_RETRIES}): {reason}")

                if attempt < _MAX_FIX_RETRIES:
                    messages.append({"role": "assistant", "content": raw})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"代码有错误，请修正：\n{reason}\n\n"
                            f"只输出完整 Python 代码，"
                            f"用 ```python ... ``` 包裹。"
                        ),
                    })
            except Exception:
                logger.exception(
                    f"Candidate generation attempt {attempt + 1} failed")
                break

        return False
