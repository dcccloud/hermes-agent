"""
Device instrumentation — wraps u2.Device to record every ADB-level call.

Usage in executor:
    recorder = DeviceRecorder(device)
    context = ExecutionContext(device=recorder, agent=agent, params=params)
    result = op_instance.execute(context)
    result.data["adb_commands"] = recorder.drain()
"""
import time
from typing import Any, Dict, List, Optional


class CommandRecord:
    """A single recorded device/agent command."""

    __slots__ = ("command", "args", "success", "elapsed_ms", "error", "is_vlm")

    def __init__(
        self,
        command: str,
        args: Dict[str, Any],
        success: bool = True,
        elapsed_ms: int = 0,
        error: Optional[str] = None,
        is_vlm: bool = False,
    ):
        self.command = command
        self.args = args
        self.success = success
        self.elapsed_ms = elapsed_ms
        self.error = error
        self.is_vlm = is_vlm

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "command": self.command,
            "args": self.args,
            "success": self.success,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.error:
            d["error"] = self.error
        if self.is_vlm:
            d["is_vlm"] = True
        return d


# Methods on u2.Device that we want to record
_TRACKED_METHODS = frozenset({
    "click",
    "double_click",
    "long_click",
    "swipe",
    "swipe_points",
    "drag",
    "press",
    "screenshot",
    "window_size",
    "app_current",
    "app_start",
    "app_stop",
    "shell",
    "send_keys",
    "set_clipboard",
    "open_url",
})


class DeviceRecorder:
    """
    Transparent proxy around u2.Device that records method calls.

    Attribute access and non-tracked methods are forwarded directly.
    Callable selectors (device(text="...")) are also forwarded (not recorded).
    """

    def __init__(self, device: Any):
        # Use object.__setattr__ to avoid triggering __setattr__ proxy
        object.__setattr__(self, "_device", device)
        object.__setattr__(self, "_records", [])

    def drain(self) -> List[Dict[str, Any]]:
        """Return all recorded commands and clear the buffer."""
        records = object.__getattribute__(self, "_records")
        result = [r.to_dict() for r in records]
        records.clear()
        return result

    @property
    def records(self) -> List[CommandRecord]:
        return object.__getattribute__(self, "_records")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Forward selector calls like device(text="确定")."""
        device = object.__getattribute__(self, "_device")
        return device(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        device = object.__getattribute__(self, "_device")
        attr = getattr(device, name)

        if name in _TRACKED_METHODS and callable(attr):
            records = object.__getattribute__(self, "_records")

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                call_args = _serialize_args(name, args, kwargs)
                start = time.monotonic()
                try:
                    result = attr(*args, **kwargs)
                    elapsed = int((time.monotonic() - start) * 1000)
                    records.append(
                        CommandRecord(
                            command=name,
                            args=call_args,
                            success=True,
                            elapsed_ms=elapsed,
                        )
                    )
                    return result
                except Exception as e:
                    elapsed = int((time.monotonic() - start) * 1000)
                    records.append(
                        CommandRecord(
                            command=name,
                            args=call_args,
                            success=False,
                            elapsed_ms=elapsed,
                            error=str(e)[:200],
                        )
                    )
                    raise

            return wrapper

        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        device = object.__getattribute__(self, "_device")
        setattr(device, name, value)


class AgentRecorder:
    """
    Transparent proxy around PhoneAgent that records VLM calls.

    After agent.run() completes, reads agent.last_action_trace to capture
    the per-step action trace (coordinates, thinking, action types) for
    the self-evolving RPA learning pipeline.
    """

    def __init__(self, agent: Any):
        object.__setattr__(self, "_agent", agent)
        object.__setattr__(self, "_records", [])

    def drain(self) -> List[Dict[str, Any]]:
        records = object.__getattribute__(self, "_records")
        result = [r.to_dict() for r in records]
        records.clear()
        return result

    @property
    def records(self) -> List[CommandRecord]:
        return object.__getattribute__(self, "_records")

    def __getattr__(self, name: str) -> Any:
        agent = object.__getattribute__(self, "_agent")
        attr = getattr(agent, name)

        if name == "run" and callable(attr):
            records = object.__getattribute__(self, "_records")

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                prompt_preview = ""
                if args:
                    prompt_preview = str(args[0])[:100]
                start = time.monotonic()
                try:
                    result = attr(*args, **kwargs)
                    elapsed = int((time.monotonic() - start) * 1000)

                    # Capture per-step action trace from the agent
                    action_trace: List[Dict[str, Any]] = []
                    try:
                        action_trace = agent.last_action_trace
                    except AttributeError:
                        pass

                    record_args: Dict[str, Any] = {
                        "prompt_preview": prompt_preview,
                    }
                    if action_trace:
                        record_args["action_trace"] = action_trace

                    records.append(
                        CommandRecord(
                            command="agent.run",
                            args=record_args,
                            success=True,
                            elapsed_ms=elapsed,
                            is_vlm=True,
                        )
                    )
                    return result
                except Exception as e:
                    elapsed = int((time.monotonic() - start) * 1000)

                    action_trace = []
                    try:
                        action_trace = agent.last_action_trace
                    except AttributeError:
                        pass

                    record_args = {"prompt_preview": prompt_preview}
                    if action_trace:
                        record_args["action_trace"] = action_trace

                    records.append(
                        CommandRecord(
                            command="agent.run",
                            args=record_args,
                            success=False,
                            elapsed_ms=elapsed,
                            error=str(e)[:200],
                            is_vlm=True,
                        )
                    )
                    raise

            return wrapper

        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        agent = object.__getattribute__(self, "_agent")
        setattr(agent, name, value)


def _serialize_args(method: str, args: tuple, kwargs: dict) -> Dict[str, Any]:
    """Serialize method arguments for logging, keeping it compact."""
    result: Dict[str, Any] = {}

    if method in ("click", "double_click", "long_click"):
        if len(args) >= 2:
            result["x"] = args[0]
            result["y"] = args[1]
        if "duration" in kwargs:
            result["duration"] = kwargs["duration"]

    elif method == "swipe":
        if len(args) >= 4:
            result["from"] = [args[0], args[1]]
            result["to"] = [args[2], args[3]]
        if len(args) >= 5:
            result["duration"] = args[4]
        if "duration" in kwargs:
            result["duration"] = kwargs["duration"]

    elif method == "swipe_points":
        if args:
            points = args[0]
            result["points_count"] = len(points) if hasattr(points, "__len__") else "?"
            if hasattr(points, "__len__") and len(points) >= 2:
                result["from"] = list(points[0])
                result["to"] = list(points[-1])
        if "duration" in kwargs or (len(args) >= 2):
            result["duration"] = kwargs.get("duration", args[1] if len(args) >= 2 else None)

    elif method == "screenshot":
        result["action"] = "capture"

    elif method == "shell":
        if args:
            cmd = args[0]
            result["cmd"] = str(cmd)[:100] if cmd else ""

    elif method in ("app_start", "app_stop"):
        if args:
            result["package"] = args[0]

    elif method == "send_keys":
        if args:
            text = str(args[0])
            result["text"] = text[:50] + ("..." if len(text) > 50 else "")

    return result
