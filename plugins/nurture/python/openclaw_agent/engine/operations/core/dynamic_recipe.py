"""Generic Operation wrapper for learned recipes.

Loads a recipe from RecipeStore by ``recipe_operation`` + ``recipe_step``
and executes it via AdaptiveStep.  When the learned recipe fails,
AdaptiveStep automatically falls back to VLM using the ``vlm_prompt``
stored in the recipe's sidecar ``meta.json``.

This class is referenced by ``/api/capabilities`` for dynamically
discovered (``source: "learned"``) operations and by ``/api/execute``
when it detects a non-class-path operation identifier.
"""

from openclaw_agent.engine.core.operation import ExecutionContext, Operation, OperationResult
from openclaw_agent.engine.learning.adaptive_step import AdaptiveStep
from openclaw_agent.engine.learning.recipe_store import RecipeStore


class DynamicRecipeOperation(Operation):
    """Execute a dynamically learned recipe through AdaptiveStep."""

    REQUIRED_PARAMS = ["recipe_operation"]

    def execute(self, context: ExecutionContext) -> OperationResult:
        recipe_op: str = context.params["recipe_operation"]
        recipe_step: str = context.params.get("recipe_step", "main")

        store = RecipeStore()
        meta = store.read_meta(recipe_op, recipe_step)

        vlm_prompt = meta.get(
            "vlm_prompt",
            f"完成操作: {recipe_op}/{recipe_step}",
        )
        target_page = meta.get("target_page")

        step = AdaptiveStep(recipe_op, recipe_step, target_page=target_page)
        ok = step.run(
            device=context.device,
            agent=context.agent,
            rpa_fn=lambda: False,
            vlm_prompt=vlm_prompt,
        )

        if not ok:
            return self.failed(
                f"Recipe execution failed: {recipe_op}/{recipe_step}")

        return self.success({"recipe_operation": recipe_op, "step": recipe_step})
