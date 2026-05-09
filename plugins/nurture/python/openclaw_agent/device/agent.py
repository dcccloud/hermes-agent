"""Main PhoneAgent class for orchestrating phone automation."""

import json
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from openclaw_agent.device.actions import ActionHandler
from openclaw_agent.device.actions.handler import do, finish, parse_action
from openclaw_agent.device.config import get_messages, get_system_prompt
from openclaw_agent.device.device_factory import get_device_factory
from openclaw_agent.device.model import ModelClient, ModelConfig
from openclaw_agent.device.model.client import MessageBuilder


@dataclass
class AgentConfig:
    """Configuration for the PhoneAgent."""

    max_steps: int = 100
    device_id: str | None = None
    lang: str = "cn"
    system_prompt: str | None = None
    verbose: bool = True

    def __post_init__(self):
        if self.system_prompt is None:
            self.system_prompt = get_system_prompt(self.lang)


@dataclass
class StepResult:
    """Result of a single agent step."""

    success: bool
    finished: bool
    action: dict[str, Any] | None
    thinking: str
    message: str | None = None


class PhoneAgent:
    """
    AI-powered agent for automating Android phone interactions.

    The agent uses a vision-language model to understand screen content
    and decide on actions to complete user tasks.

    Args:
        model_config: Configuration for the AI model.
        agent_config: Configuration for the agent behavior.
        confirmation_callback: Optional callback for sensitive action confirmation.
        takeover_callback: Optional callback for takeover requests.

    Example:
        >>> from openclaw_agent.device import PhoneAgent
        >>> from openclaw_agent.device.model import ModelConfig
        >>>
        >>> model_config = ModelConfig(base_url="http://localhost:8000/v1")
        >>> agent = PhoneAgent(model_config)
        >>> agent.run("Open WeChat and send a message to John")
    """

    def __init__(
        self,
        model_config: ModelConfig | None = None,
        agent_config: AgentConfig | None = None,
        confirmation_callback: Callable[[str], bool] | None = None,
        takeover_callback: Callable[[str], None] | None = None,
    ):
        self.model_config = model_config or ModelConfig()
        self.agent_config = agent_config or AgentConfig()

        self.model_client = ModelClient(self.model_config)
        self.action_handler = ActionHandler(
            device_id=self.agent_config.device_id,
            confirmation_callback=confirmation_callback,
            takeover_callback=takeover_callback,
        )

        self._context: list[dict[str, Any]] = []
        self._step_count = 0
        self._action_trace: list[dict[str, Any]] = []
        # Optional callback fired after each VLM step trace_entry is
        # appended. Used by the resume system to flush per-step traces
        # to disk so a kill mid-VLM does not lose exploration history.
        # Signature: (trace_entry: dict) -> None
        self._step_callback: Callable[[dict[str, Any]], None] | None = None
        # Optional prior-context prompt fragment to prepend to the next
        # task. Set by the executor when this run is a continuation of
        # a previous (timed-out) task.
        self._prior_context: str | None = None

    @property
    def last_action_trace(self) -> list[dict[str, Any]]:
        """Return the action trace from the most recent run() call."""
        return self._action_trace.copy()

    def set_step_callback(
        self, callback: Callable[[dict[str, Any]], None] | None
    ) -> None:
        """Register/clear a per-step callback fired after each VLM step.

        Used by the executor to flush per-step trace entries to disk for
        resumable execution. Pass ``None`` to clear.
        """
        self._step_callback = callback

    def set_prior_context(self, prior_context: str | None) -> None:
        """Register/clear a prior-attempt context fragment.

        When set, the next ``run(task)`` call prepends this text to the
        task prompt so the VLM benefits from prior exploration history
        across a task continuation.
        """
        self._prior_context = prior_context

    def run(self, task: str) -> str:
        """
        Run the agent to complete a task.

        Args:
            task: Natural language description of the task.

        Returns:
            Final message from the agent.
        """
        self._context = []
        self._step_count = 0
        self._action_trace = []

        # Prepend prior-attempt context for task continuation, if any.
        if self._prior_context:
            task = f"{self._prior_context}\n\n[Original task]\n{task}"

        # First step with user prompt
        result = self._execute_step(task, is_first=True)

        if result.finished:
            return result.message or "Task completed"

        # Continue until finished or max steps reached
        while self._step_count < self.agent_config.max_steps:
            result = self._execute_step(is_first=False)

            if result.finished:
                return result.message or "Task completed"

        return "Max steps reached"

    def run_with_details(self, task: str) -> dict[str, str]:
        """
        Run the agent to complete a task, returning both message and thinking.

        Args:
            task: Natural language description of the task.

        Returns:
            Dict with 'message' and 'thinking' fields.
        """
        self._context = []
        self._step_count = 0
        self._action_trace = []

        # Prepend prior-attempt context for task continuation, if any.
        if self._prior_context:
            task = f"{self._prior_context}\n\n[Original task]\n{task}"

        # First step with user prompt
        result = self._execute_step(task, is_first=True)

        if result.finished:
            return {
                "message": result.message or "Task completed",
                "thinking": result.thinking
            }

        # Continue until finished or max steps reached
        while self._step_count < self.agent_config.max_steps:
            result = self._execute_step(is_first=False)

            if result.finished:
                return {
                    "message": result.message or "Task completed",
                    "thinking": result.thinking
                }

        return {
            "message": "Max steps reached",
            "thinking": result.thinking if 'result' in locals() else ""
        }

    def step(self, task: str | None = None) -> StepResult:
        """
        Execute a single step of the agent.

        Useful for manual control or debugging.

        Args:
            task: Task description (only needed for first step).

        Returns:
            StepResult with step details.
        """
        is_first = len(self._context) == 0

        if is_first and not task:
            raise ValueError("Task is required for the first step")

        return self._execute_step(task, is_first)

    def reset(self) -> None:
        """Reset the agent state for a new task."""
        self._context = []
        self._step_count = 0
        self._action_trace = []

    def _execute_step(
        self, user_prompt: str | None = None, is_first: bool = False
    ) -> StepResult:
        """Execute a single step of the agent loop."""
        self._step_count += 1

        # Capture current screen state
        device_factory = get_device_factory()
        screenshot = device_factory.get_screenshot(self.agent_config.device_id)
        current_app = device_factory.get_current_app(self.agent_config.device_id)

        # Build messages
        if is_first:
            self._context.append(
                MessageBuilder.create_system_message(self.agent_config.system_prompt)
            )

            screen_info = MessageBuilder.build_screen_info(current_app)
            text_content = f"{user_prompt}\n\n{screen_info}"

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )
        else:
            screen_info = MessageBuilder.build_screen_info(current_app)
            text_content = f"** Screen Info **\n\n{screen_info}"

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )

        # Get model response
        try:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "=" * 50)
            print(f"💭 {msgs['thinking']}:")
            print("-" * 50)
            response = self.model_client.request(self._context)
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            return StepResult(
                success=False,
                finished=True,
                action=None,
                thinking="",
                message=f"Model error: {e}",
            )

        # Parse action from response
        try:
            action = parse_action(response.action)
        except ValueError:
            if self.agent_config.verbose:
                traceback.print_exc()
            action = finish(message=response.action)

        if self.agent_config.verbose:
            # Print thinking process
            print("-" * 50)
            print(f"🎯 {msgs['action']}:")
            print(json.dumps(action, ensure_ascii=False, indent=2))
            print("=" * 50 + "\n")

        # Remove image from context to save space
        self._context[-1] = MessageBuilder.remove_images_from_message(self._context[-1])

        # Execute action
        try:
            result = self.action_handler.execute(
                action, screenshot.width, screenshot.height
            )
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            result = self.action_handler.execute(
                finish(message=str(e)), screenshot.width, screenshot.height
            )

        # Record action trace for self-evolving RPA learning
        trace_entry: dict[str, Any] = {
            "step": self._step_count,
            "action_type": action.get("action", action.get("_metadata", "unknown")),
            "thinking": response.thinking,
            "success": result.success,
            "screen_width": screenshot.width,
            "screen_height": screenshot.height,
        }
        if "element" in action:
            trace_entry["element"] = action["element"]
        if "element_region" in action:
            trace_entry["element_region"] = action["element_region"]
        if "start" in action:
            trace_entry["start"] = action["start"]
        if "end" in action:
            trace_entry["end"] = action["end"]
        if "text" in action:
            trace_entry["text"] = action["text"]
        if "message" in action and action.get("_metadata") == "finish":
            trace_entry["finish_message"] = action["message"]
        self._action_trace.append(trace_entry)

        # Optional incremental flush (resumable execution). Best-effort —
        # callback failures are silenced so they cannot break the VLM loop.
        if self._step_callback is not None:
            try:
                self._step_callback(trace_entry)
            except Exception:
                if self.agent_config.verbose:
                    traceback.print_exc()

        # Add assistant response to context
        self._context.append(
            MessageBuilder.create_assistant_message(
                f"<think>{response.thinking}</think><answer>{response.action}</answer>"
            )
        )

        # Check if finished
        finished = action.get("_metadata") == "finish" or result.should_finish

        if finished and self.agent_config.verbose:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "🎉 " + "=" * 48)
            print(
                f"✅ {msgs['task_completed']}: {result.message or action.get('message', msgs['done'])}"
            )
            print("=" * 50 + "\n")

        return StepResult(
            success=result.success,
            finished=finished,
            action=action,
            thinking=response.thinking,
            message=result.message or action.get("message"),
        )

    @property
    def context(self) -> list[dict[str, Any]]:
        """Get the current conversation context."""
        return self._context.copy()

    @property
    def step_count(self) -> int:
        """Get the current step count."""
        return self._step_count
