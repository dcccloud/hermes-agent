import threading
import time
import importlib
from datetime import datetime, timezone
from typing import Dict, Any, Type, List, Callable, Optional
from openclaw_agent.engine.common import get_logger, set_task_id, clear_task_id
from openclaw_agent.engine.common.config import get_config
from openclaw_agent.engine.devices.device_manager import device_manager
from openclaw_agent.engine.agents.phone_agent_pool import PhoneAgentPool
from openclaw_agent.engine.core.validator import Validator
from openclaw_agent.engine.core.operation import ExecutionContext, OperationResult, Operation
from openclaw_agent.engine.learning.directive_context import load_active_directives
from openclaw_agent.engine.learning.event_log import emit as emit_event
from openclaw_agent.engine.learning.checkpoint_store import (
    CheckpointStore,
    OP_STATUS_FAILED,
    OP_STATUS_SUCCESS,
    compute_op_params_hash,
)
from openclaw_agent.engine.learning.continuation_context import (
    build_prior_context_prompt,
)
from openclaw_agent.engine.learning.trace_store import (
    InFlightTraceWriter,
    archive_in_flight,
    read_in_flight_steps,
)
from openclaw_agent.engine.utils.instrumentation import DeviceRecorder, AgentRecorder


logger = get_logger("executor")

# Well-known app package → short name mapping
_APP_PACKAGE_MAP: Dict[str, str] = {
    "com.ss.android.ugc.aweme": "douyin",
    "com.tencent.mm": "wechat",
    "com.xingin.xhs": "xiaohongshu",
}


class OperationExecutor:
    """任务执行器"""
    
    def __init__(self, agent_pool: PhoneAgentPool):
        """
        初始化执行器

        Args:
            agent_pool: Agent 池
        """
        self.agent_pool = agent_pool
        self.validator = Validator()

    @property
    def checkpoint_store(self) -> CheckpointStore:
        """Lazy CheckpointStore — instantiated per access so the data
        directory honors any ``_configure_workspace`` patches applied
        AFTER the executor was constructed (server.py module-init order).
        """
        return CheckpointStore()
    
    def _load_operation_class(self, handler_class: str) -> Type[Operation]:
        """
        加载 Operation 类
        
        :param handler_class: 类路径，如 "operations.haosheng.open_app.HaoshengOpenAppOperation"
        :return: Operation 类
        :raises ValueError: 加载失败时抛出
        """
        try:
            module_path, class_name = handler_class.rsplit('.', 1)
            module = importlib.import_module(f"openclaw_agent.engine.{module_path}")
            operation_class = getattr(module, class_name)
            
            if not issubclass(operation_class, Operation):
                raise ValueError(f"'{handler_class}' is not a valid Operation subclass")
            
            return operation_class
            
        except (ValueError, AttributeError, ModuleNotFoundError) as e:
            raise ValueError(f"Failed to load operation class '{handler_class}': {e}") from e
    
    def _validate_all_operations(self, operations: List[Dict[str, Any]]) -> List[str]:
        """
        预校验所有操作的参数
        
        :param operations: 操作列表
        :return: 错误信息列表（空列表表示全部通过）
        """
        errors = []
        
        for op_config in operations:
            op_name = op_config.get("name", "unknown")
            handler_class = op_config.get("handler_class")
            op_params = op_config.get("params", {})
            
            if not handler_class:
                errors.append(f"Operation '{op_name}': 缺少 handler_class")
                continue
            
            try:
                # 加载并实例化Operation类
                op_class = self._load_operation_class(handler_class)
                op_instance = op_class()
                
                # 调用validate方法
                valid, error_msg = op_instance.validate(op_params)
                if not valid:
                    errors.append(f"Operation '{op_name}': {error_msg}")
                    
            except Exception as e:
                errors.append(f"Operation '{op_name}': 加载失败 - {str(e)}")
        
        return errors
    
    def execute_task(self, task_message: Dict[str, Any], operation_callback: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """
        执行任务
        
        Args:
            task_message: 任务消息
            operation_callback: 每个 operation 执行完后的回调函数，接收 operation 结果消息
            
        Returns:
            任务执行结果
        """
        task_id = task_message["task_id"]
        task_type = task_message.get("task_type", "unknown")
        device_id = task_message.get("device_id", "unknown")
        operations = task_message["operations"]
        is_continuation = bool(task_message.get("is_continuation"))

        # 设置当前任务的 task_id 到日志上下文
        set_task_id(task_id)

        logger.info(
            f"开始执行任务, 类型: {task_type}, 设备: {device_id}, "
            f"is_continuation={is_continuation}"
        )

        # 预校验所有操作的参数
        validation_errors = self._validate_all_operations(operations)
        if validation_errors:
            error_msg = "; ".join(validation_errors)
            logger.error(f"任务参数校验失败: {error_msg}")
            clear_task_id()
            return {
                "task_id": task_id,
                "task_type": task_type,
                "success": False,
                "result": {
                    "error": f"参数校验失败: {error_msg}",
                    "operations_result": [],
                    "total_duration_ms": 0,
                    "device_id": device_id
                }
            }
        
        logger.info("✓ 参数校验通过")
        
        device = device_manager.get(device_id)

        # Load active community directives for this task
        active_directives = load_active_directives()

        # ── Resume support: only for tasks containing NL operations.
        #    Simple recipe operations (swipe, tap, etc.) should always
        #    execute, even if called with the same task_id.
        resume_cfg = get_config().resume
        resume_info: Dict[str, Any] = {
            "skip_indices": set(),
            "skip_results": {},
            "first_unmatched": None,
            "mismatch": False,
        }
        _has_nl_op = any(
            op.get("operation_class")
            == "operations.core.natural_language.NaturalLanguageOperation"
            for op in operations
        )
        if resume_cfg.enabled and _has_nl_op:
            resume_info = self.checkpoint_store.compute_resume_point(
                task_id=task_id,
                requested_ops=operations,
                strict=(resume_cfg.op_signature_mismatch_policy == "strict"),
            )
            if resume_info["skip_indices"]:
                logger.info(
                    "Resume: skipping %d already-completed op(s): %s",
                    len(resume_info["skip_indices"]),
                    sorted(resume_info["skip_indices"]),
                )
            if resume_info["mismatch"]:
                logger.warning(
                    "Resume: op signature mismatch detected (policy=%s, "
                    "first_unmatched=%s)",
                    resume_cfg.op_signature_mismatch_policy,
                    resume_info["first_unmatched"],
                )

        # ── In-flight trace + prior-context: capture VLM steps to disk
        #    incrementally so a kill mid-VLM does not lose exploration
        #    history; on continuation, surface that history to the next
        #    VLM call as a prompt prefix.
        in_flight_writer: Optional[InFlightTraceWriter] = None
        prior_context: Optional[str] = None
        agents_with_callback: List[Any] = []
        if resume_cfg.enabled and resume_cfg.in_flight_trace_enabled:
            try:
                if is_continuation:
                    prior_steps = read_in_flight_steps(task_id)
                    if prior_steps:
                        prior_context = build_prior_context_prompt(
                            prior_steps,
                            max_actions=resume_cfg.prior_context_max_actions,
                        )
                        logger.info(
                            "Resume: loaded %d prior step(s) as prior_context",
                            len(prior_steps),
                        )
                in_flight_writer = InFlightTraceWriter(task_id, device_id)
            except Exception:
                logger.exception("Resume: in-flight trace setup failed")
                in_flight_writer = None
                prior_context = None

        results = []
        total_start = time.time()

        for op_index, op_config in enumerate(operations):
            op_name = op_config["name"]
            op_params = op_config.get("params", {})
            display_name = op_config.get("display_name", op_name)

            # ── Resume: cached completion → emit-and-continue ──
            if op_index in resume_info["skip_indices"]:
                cached_result = resume_info["skip_results"].get(op_index, {})
                op_result = {
                    "name": op_name,
                    "status": cached_result.get("status", "success"),
                    "duration_ms": 0,
                    "data": cached_result.get("data") or {},
                    "error": cached_result.get("error"),
                    "resumed_from_checkpoint": True,
                }
                results.append(op_result)
                logger.info(
                    f"Operation {op_name} (op_index={op_index}) skipped via "
                    f"checkpoint"
                )
                emit_event("operation.skipped", {
                    "op_index": op_index,
                    "reason": "checkpoint",
                }, device_id=device_id, operation=op_name)
                if operation_callback:
                    try:
                        operation_callback({
                            "task_id": task_id,
                            "task_type": task_type,
                            "op_index": op_index,
                            "op_name": op_name,
                            "display_name": display_name,
                            "success": True,
                            "result": op_result["data"],
                            "resumed_from_checkpoint": True,
                        })
                    except Exception as e:
                        logger.error(f"Operation 结果回调失败: {e}")
                continue

            op_started_iso = datetime.now(timezone.utc).isoformat()

            try:
                # 加载 Operation 类
                handler_class = op_config.get("handler_class")
                if not handler_class:
                    error_msg = f"Operation '{op_name}' missing 'handler_class' field"
                    logger.error(error_msg)
                    raise ValueError(error_msg)

                logger.debug(f"Loading operation class: {handler_class}")
                op_class = self._load_operation_class(handler_class)
                op_instance = op_class()
                
                # 获取 operation 自定义的 max_steps（如果有）
                custom_max_steps = getattr(op_instance, 'MAX_STEPS', None)
                
                # 每个operation执行前重置agent上下文，避免历史对话干扰
                # 优先使用 operation 自定义的 max_steps，否则使用默认值
                agent = self.agent_pool.get(device_id, reset_context=True, max_steps=custom_max_steps)

                # Wire up per-step in-flight flush + prior-context for resume.
                # Set unconditionally each iteration so leftover state from a
                # previous (different) task on this cached agent is overwritten.
                self._configure_agent_for_resume(
                    agent=agent,
                    in_flight_writer=in_flight_writer,
                    op_index=op_index,
                    op_name=op_name,
                    prior_context=prior_context,
                    tracked=agents_with_callback,
                )

                # For NL operations, layer a page-change tracker on top of
                # the existing step callback so the app graph grows in real
                # time as the VLM navigates across pages.
                is_nl_op = (
                    handler_class
                    == "operations.core.natural_language"
                       ".NaturalLanguageOperation"
                )
                page_tracker = None
                if is_nl_op and hasattr(agent, "set_step_callback"):
                    page_tracker = self._layer_page_tracker(agent, device, device_id, agents_with_callback)

                if custom_max_steps:
                    logger.debug(f"Operation {op_name} 使用自定义 max_steps: {custom_max_steps}")
                
                # if not op_instance.validate(op_params):
                #     results.append({
                #         "name": op_name,
                #         "status": "skipped",
                #         "duration_ms": 0,
                #         "error": "准入条件不满足"
                #     })
                #     continue
                
                device_recorder = DeviceRecorder(device)
                agent_recorder = AgentRecorder(agent)

                context = ExecutionContext(
                    device=device_recorder,
                    agent=agent_recorder,
                    params=op_params,
                    directives=active_directives,
                )

                # 前置条件检查
                pre_ok, pre_err = op_instance.check_precondition(
                    context, op_config)
                if not pre_ok:
                    logger.warning(
                        f"Operation {op_name} 前置检查失败: {pre_err}")

                    # 尝试状态恢复后重新检查前置条件
                    try:
                        recovered = op_instance.recover_state(context)
                        if recovered:
                            pre_ok, pre_err = op_instance.check_precondition(
                                context, op_config)
                            if pre_ok:
                                logger.info(
                                    f"Operation {op_name} 状态恢复后前置检查通过")
                                emit_event("precondition.recovered", {},
                                           device_id=device_id, operation=op_name)
                    except Exception as recover_e:
                        logger.warning(f"前置检查失败后状态恢复异常: {recover_e}")

                if not pre_ok:
                    emit_event("precondition.failed", {
                        "reason": pre_err or "unknown",
                    }, device_id=device_id, operation=op_name)
                    op_result = {
                        "name": op_name,
                        "status": "failed",
                        "duration_ms": 0,
                        "data": None,
                        "error": f"前置检查失败: {pre_err}",
                    }
                    results.append(op_result)
                    self._record_op_checkpoint(
                        task_id=task_id, op_index=op_index, op_name=op_name,
                        op_params=op_params, op_result=op_result,
                        device_id=device_id, op_started_iso=op_started_iso,
                        has_nl_op=_has_nl_op,
                    )
                    if operation_callback:
                        try:
                            operation_callback({
                                "task_id": task_id,
                                "task_type": task_type,
                                "op_index": op_index,
                                "op_name": op_name,
                                "display_name": display_name,
                                "success": False,
                                "result": {"error": f"前置检查失败: {pre_err}"},
                            })
                        except Exception as e:
                            logger.error(
                                f"Operation 结果回调失败: {e}")
                    break

                emit_event("operation.started", {
                    "handler_class": handler_class,
                    "op_index": op_index,
                }, device_id=device_id, operation=op_name)

                start = time.time()
                result = op_instance.execute(context)
                result.duration_ms = int((time.time() - start) * 1000)

                # 后置条件检查（仅在 execute 成功时）
                if result.status == "success":
                    post_ok, post_err = op_instance.check_postcondition(
                        context, op_config)
                    if not post_ok:
                        logger.warning(
                            f"Operation {op_name} 后置检查失败: {post_err}")
                        result.status = "failed"
                        result.error = f"后置检查失败: {post_err}"
                        result.should_continue = False

                adb_commands = device_recorder.drain() + agent_recorder.drain()

                op_data = result.data or {}
                op_data["adb_commands"] = adb_commands
                vlm_calls = [c for c in adb_commands if c.get("is_vlm")]
                if vlm_calls:
                    op_data["vlm_used"] = True
                    op_data["vlm_call_count"] = len(vlm_calls)

                op_result = {
                    "name": op_name,
                    "status": result.status,
                    "duration_ms": result.duration_ms,
                    "data": op_data,
                    "error": result.error
                }
                results.append(op_result)
                self._record_op_checkpoint(
                    task_id=task_id, op_index=op_index, op_name=op_name,
                    op_params=op_params, op_result=op_result,
                    device_id=device_id, op_started_iso=op_started_iso,
                    has_nl_op=_has_nl_op,
                )

                logger.info(f"Operation {op_name} 执行完成: {result.status}")

                emit_event("operation.completed", {
                    "status": result.status,
                    "duration_ms": result.duration_ms,
                    "error": result.error,
                    "data": result.data,
                    "vlm_used": bool(vlm_calls),
                    "vlm_call_count": len(vlm_calls),
                }, device_id=device_id, operation=op_name)

                # NL trace persistence: every NL run with VLM activity
                # produces a trace, regardless of outcome.
                #   - success           → _try_learn_from_nl (may stitch
                #                          a prior max_steps trace and
                #                          generate a recipe)
                #   - failed (max_steps)→ _save_partial_nl_trace as a
                #                          continuation candidate
                #   - failed (other)    → _save_partial_nl_trace with
                #                          outcome="vlm_error"
                is_nl = (
                    handler_class
                    == "operations.core.natural_language"
                       ".NaturalLanguageOperation"
                )
                if is_nl and vlm_calls:
                    if result.status == "success":
                        self._try_learn_from_nl(
                            prompt=op_params.get("prompt", ""),
                            vlm_calls=vlm_calls,
                            device=device,
                            device_id=device_id,
                        )
                    else:
                        self._save_partial_nl_trace(
                            prompt=op_params.get("prompt", ""),
                            vlm_calls=vlm_calls,
                            device=device,
                            device_id=device_id,
                            error=result.error or "",
                        )

                # 调用回调发布 operation 结果
                if operation_callback:
                    try:
                        op_message = {
                            "task_id": task_id,
                            "task_type": task_type,
                            "op_index": op_index,
                            "op_name": op_name,
                            "display_name": display_name,
                            "success": result.status == "success",
                            "result": result.data or {}
                        }
                        operation_callback(op_message)
                    except Exception as e:
                        logger.error(f"Operation 结果回调失败: {e}")

                # 检查是否需要停止后续执行
                if not result.should_continue:
                    logger.info(f"Operation {op_name} 失败，尝试状态恢复...")
                    try:
                        recovered = op_instance.recover_state(context)
                        if recovered:
                            logger.info(f"状态恢复成功，继续执行后续操作")
                            continue
                    except Exception as recover_e:
                        logger.warning(f"状态恢复异常: {recover_e}")
                    logger.warning(f"状态恢复失败，终止任务执行")
                    break

            except Exception as e:
                logger.error(f"Operation {op_name} 执行失败: {str(e)}")

                # Drain recorders even on exception to avoid data loss
                try:
                    adb_commands = device_recorder.drain() + agent_recorder.drain()
                except Exception:
                    adb_commands = []

                duration_ms = int((time.time() - start) * 1000) if 'start' in locals() else 0

                op_result = {
                    "name": op_name,
                    "status": "failed",
                    "duration_ms": duration_ms,
                    "error": str(e)
                }
                results.append(op_result)
                self._record_op_checkpoint(
                    task_id=task_id, op_index=op_index, op_name=op_name,
                    op_params=op_params, op_result=op_result,
                    device_id=device_id, op_started_iso=op_started_iso,
                    has_nl_op=_has_nl_op,
                )

                emit_event("operation.completed", {
                    "status": "error",
                    "duration_ms": duration_ms,
                    "error": str(e),
                }, device_id=device_id, operation=op_name)

                # 调用回调发布 operation 结果
                if operation_callback:
                    try:
                        op_message = {
                            "task_id": task_id,
                            "task_type": task_type,
                            "op_index": op_index,
                            "op_name": op_name,
                            "display_name": display_name,
                            "success": False,
                            "result": {"error": str(e)}
                        }
                        operation_callback(op_message)
                    except Exception as callback_e:
                        logger.error(f"Operation 结果回调失败: {callback_e}")

                # 尝试状态恢复后继续
                try:
                    recovered = op_instance.recover_state(context)
                    if recovered:
                        logger.info(f"Operation {op_name} 异常后状态恢复成功，继续执行")
                        continue
                except Exception:
                    pass
                logger.warning(f"Operation {op_name} 异常失败，终止任务执行")
                break
        
        total_duration = int((time.time() - total_start) * 1000)
        all_success = all(r["status"] == "success" for r in results)

        # Clear resume-related state from cached agents and archive the
        # in-flight trace if (and only if) every op succeeded — otherwise
        # the file stays in place for the next continuation attempt.
        self._clear_agents_resume_state(agents_with_callback)
        if in_flight_writer is not None:
            try:
                in_flight_writer.close()
            except Exception:
                pass
            if all_success:
                try:
                    archive_in_flight(task_id)
                except Exception:
                    logger.exception(
                        "Resume: archive_in_flight failed for task=%s", task_id
                    )

        # 返回格式（参考 mock_agent observations 队列的格式）
        result = {
            "task_id": task_id,
            "task_type": task_type,
            "success": all_success,
            "result": {
                "operations_result": results,
                "total_duration_ms": total_duration,
                "device_id": device_id
            }
        }
        
        # 注意：不在这里清除 task_id，由 consumer 发布完最终结果后再清除
        
        return result

    # -- Resume / checkpoint helpers ----------------------------------------

    def _configure_agent_for_resume(
        self,
        agent: Any,
        in_flight_writer: Optional["InFlightTraceWriter"],
        op_index: int,
        op_name: str,
        prior_context: Optional[str],
        tracked: List[Any],
    ) -> None:
        """Wire per-op step callback + prior-context onto a PhoneAgent.

        ``set_step_callback`` and ``set_prior_context`` are best-effort —
        agents that don't expose them (older versions, mocks) are
        silently left alone. Tracked agent instances are recorded so the
        executor can clear callbacks at task end and avoid leaks across
        tasks via the cached agent pool.
        """
        # Per-step flush callback (closure captures op_index/op_name)
        if in_flight_writer is not None and hasattr(agent, "set_step_callback"):
            try:
                agent.set_step_callback(
                    lambda entry, _i=op_index, _n=op_name:
                        in_flight_writer.append_step(_i, _n, entry)
                )
                if all(a is not agent for a in tracked):
                    tracked.append(agent)
            except Exception:
                logger.exception(
                    "set_step_callback failed for op_index=%d", op_index
                )
        elif hasattr(agent, "set_step_callback"):
            try:
                agent.set_step_callback(None)
            except Exception:
                pass

        # Prior-context (continuation prompt prefix)
        if hasattr(agent, "set_prior_context"):
            try:
                agent.set_prior_context(prior_context)
                if prior_context and all(a is not agent for a in tracked):
                    tracked.append(agent)
            except Exception:
                logger.exception(
                    "set_prior_context failed for op_index=%d", op_index
                )

    def _layer_page_tracker(
        self, agent: Any, device: Any, device_id: str, tracked: List[Any],
    ) -> Any:
        """Wrap the agent's existing step callback with a PageChangeTracker.

        The tracker runs a cheap perceptual-hash check after each action
        and only triggers heavier identification (indicator match / VLM
        discovery) when the page actually changes.
        """
        try:
            from openclaw_agent.device.device_factory import get_device_factory
            app = get_device_factory().get_current_app(device_id)
        except Exception:
            logger.debug("could not detect current app", exc_info=True)
            return None
        if not app or app == "System Home":
            return None
        try:
            from openclaw_agent.engine.learning.graph_updater import (
                PageChangeTracker,
            )
            tracker = PageChangeTracker(device, app)
        except Exception:
            logger.debug("failed to create PageChangeTracker", exc_info=True)
            return None

        existing_cb = getattr(agent, "_step_callback", None)

        def _combined(entry: dict) -> None:
            if existing_cb is not None:
                existing_cb(entry)
            try:
                tracker.on_step(entry)
            except Exception:
                logger.debug("page tracker error", exc_info=True)

        try:
            agent.set_step_callback(_combined)
            logger.info("[PageTracker] wired for app='%s'", app)
            if all(a is not agent for a in tracked):
                tracked.append(agent)
            return tracker
        except Exception:
            logger.debug("set_step_callback failed for page tracker",
                         exc_info=True)
            return None

    def _clear_agents_resume_state(self, tracked: List[Any]) -> None:
        """Clear step callbacks and prior context on all agents we touched.

        Without this, the next task picked up by the same cached agent
        could still write to the previous task's in-flight file.
        """
        for agent in tracked:
            try:
                if hasattr(agent, "set_step_callback"):
                    agent.set_step_callback(None)
            except Exception:
                pass
            try:
                if hasattr(agent, "set_prior_context"):
                    agent.set_prior_context(None)
            except Exception:
                pass

    def _record_op_checkpoint(
        self,
        task_id: str,
        op_index: int,
        op_name: str,
        op_params: Dict[str, Any],
        op_result: Dict[str, Any],
        device_id: str,
        op_started_iso: str,
        *,
        has_nl_op: bool = False,
    ) -> None:
        """Persist an op-level checkpoint after each completion point.

        Best-effort: any exception is swallowed (with a logged trace) so a
        checkpoint write failure never breaks task execution.
        """
        if not get_config().resume.enabled or not has_nl_op:
            return
        status = (
            OP_STATUS_SUCCESS
            if op_result.get("status") == "success"
            else OP_STATUS_FAILED
        )
        try:
            self.checkpoint_store.record_op(
                task_id=task_id,
                op_index=op_index,
                op_name=op_name,
                status=status,
                result=op_result,
                device_id=device_id,
                op_params_hash=compute_op_params_hash(op_params),
                started_at=op_started_iso,
            )
        except Exception:
            logger.exception(
                "Checkpoint write failed (task_id=%s op_index=%d)",
                task_id, op_index,
            )

    # -- NL learning helpers ------------------------------------------------

    # Continuation window: a prior max_steps trace older than this is no
    # longer treated as a continuation candidate.
    _CONTINUATION_TTL_SECONDS = 180

    def _try_learn_from_nl(
        self,
        prompt: str,
        vlm_calls: List[Dict[str, Any]],
        device: Any,
        device_id: str,
    ) -> None:
        """After a successful NaturalLanguageOperation with VLM, attempt to
        save the trace and eventually generate a reusable named recipe.

        If a recent ``max_steps`` trace exists for this device+app, its
        actions are prepended to the current trace and recorded via
        ``continuation_of`` so RecipeGenerator sees the full sequence.
        When stitching, the prior trace's operation name is reused
        verbatim so the whole chain shares one file.

        Runs entirely in a background thread so the caller returns
        immediately.
        """

        def _learn() -> None:
            try:
                # 1. Detect current foreground app
                app = _detect_app(device)
                if not app:
                    logger.debug("NL-learn: could not detect app, skipping")
                    return

                # 2. Extract action_trace from VLM recorder output
                actions: List[Dict[str, Any]] = _extract_action_trace(vlm_calls)
                if not actions:
                    logger.debug("NL-learn: no action trace, skipping")
                    return

                # 3. Look up a prior max_steps trace for this device+app.
                # If found, prepend its actions and record the link.
                from openclaw_agent.engine.learning.trace_store import TraceStore
                from openclaw_agent.engine.common import get_task_id
                trace_store = TraceStore()
                prior = trace_store.find_recent(
                    device_id=device_id,
                    outcomes=("max_steps",),
                    max_age_seconds=self._CONTINUATION_TTL_SECONDS,
                    operation_prefix=f"{app}/",
                    limit=1,
                )
                continuation_of: str | None = None
                prior_op_name: str | None = None
                if prior:
                    prev = prior[0]
                    prior_actions = prev.get("actions", [])
                    if prior_actions:
                        actions = list(prior_actions) + actions
                        continuation_of = prev.get("id")
                        prev_op = prev.get("operation", "")
                        if "/" in prev_op:
                            prior_op_name = prev_op.split("/", 1)[1]
                        logger.info(
                            f"NL-learn: stitching {len(prior_actions)} prior "
                            f"actions from max_steps trace id={continuation_of} "
                            f"(prior_op={prior_op_name})")

                # 4. Resolve op name. Prefer the prior trace's name so the
                # chain stays in one file; otherwise call operation_namer
                # and bail when LLM declines (one-off / duplicate).
                from openclaw_agent.engine.learning.recipe_store import RecipeStore
                store = RecipeStore()
                if prior_op_name and not prior_op_name.startswith("_unnamed_"):
                    op_name = prior_op_name
                    display_name = op_name
                    logger.info(
                        f"NL-learn: reusing prior op name '{op_name}' for "
                        f"continuation")
                else:
                    naming = _name_nl_operation(
                        prompt=prompt, actions=actions, app=app,
                        recipe_store=store,
                    )
                    if not naming or not naming.get("should_learn"):
                        reason = (naming or {}).get("reason", "None")
                        logger.info(f"NL-learn: LLM declined ({reason})")
                        return
                    op_name = naming["name"]
                    display_name = naming.get("display_name", op_name)

                operation = f"{app}/{op_name}"

                # 5. Save trace
                trace_store.save(
                    operation=operation,
                    step="main",
                    trace=actions,
                    device_id=device_id,
                    task_id=get_task_id() or None,
                    outcome="completed",
                    prompt=prompt,
                    continuation_of=continuation_of,
                )

                # 6. Update recipe meta (even before recipe gen)
                store.update_meta(operation, "main", {
                    "display_name": display_name,
                    "vlm_prompt": prompt,
                    "affiliated_app": app,
                    "source": "learned",
                })

                # 7. Trigger async recipe generation
                from openclaw_agent.engine.learning.recipe_generator import (
                    RecipeGenerator,
                )
                gen = RecipeGenerator()
                count = gen.process_for_step(operation, "main")
                if count:
                    logger.info(
                        f"NL-learn: generated {count} recipe(s) for "
                        f"{operation}")
                else:
                    logger.info(
                        f"NL-learn: trace saved for {operation}, "
                        f"recipe gen produced 0 (may need more traces)")

            except Exception:
                logger.exception("NL-learn: background learning failed")

        t = threading.Thread(target=_learn, daemon=True, name="nl-learn")
        t.start()

    def _save_partial_nl_trace(
        self,
        prompt: str,
        vlm_calls: List[Dict[str, Any]],
        device: Any,
        device_id: str,
        error: str,
    ) -> None:
        """Persist a NL trace whose VLM run did not finish successfully.

        Outcome is inferred from the error string:
          - ``Max steps reached`` → ``max_steps`` (continuation candidate)
          - anything else with vlm activity → ``vlm_error``

        The trace lands under the same ``<app>/<op_name>.jsonl`` file
        the eventual success would land in: we run operation_namer (a
        cheap LLM call) on the partial actions + prompt. When naming
        fails or the LLM declines, we fall back to a deterministic
        ``_unnamed_<promptHash>`` key so all attempts at the same prompt
        still aggregate into one file. Runs in a background thread.
        """

        def _save() -> None:
            try:
                actions = _extract_action_trace(vlm_calls)
                if not actions:
                    logger.debug("NL-partial: no action trace, skipping")
                    return

                app = _detect_app(device) or "unknown"
                outcome = (
                    "max_steps"
                    if "max steps reached" in error.lower()
                    else "vlm_error"
                )

                from openclaw_agent.engine.learning.recipe_store import RecipeStore
                store = RecipeStore()
                op_name = _resolve_partial_op_name(
                    prompt=prompt, actions=actions, app=app,
                    recipe_store=store,
                )

                from openclaw_agent.engine.learning.trace_store import TraceStore
                from openclaw_agent.engine.common import get_task_id
                TraceStore().save(
                    operation=f"{app}/{op_name}",
                    step="main",
                    trace=actions,
                    device_id=device_id,
                    task_id=get_task_id() or None,
                    outcome=outcome,
                    prompt=prompt,
                    extra={"error": error[:200]} if error else None,
                )
                logger.info(
                    f"NL-partial: saved {outcome} trace under "
                    f"{app}/{op_name} (actions={len(actions)})")
            except Exception:
                logger.exception("NL-partial: save failed")

        t = threading.Thread(target=_save, daemon=True, name="nl-partial")
        t.start()


def _detect_app(device: Any) -> str | None:
    """Return the short app name for the current foreground app."""
    try:
        raw = device
        # Unwrap DeviceRecorder if needed
        if hasattr(raw, "_device"):
            raw = object.__getattribute__(raw, "_device")
        info = raw.app_current()
        pkg = info.get("package", "")
        return _APP_PACKAGE_MAP.get(pkg)
    except Exception:
        return None


def _extract_action_trace(
    vlm_calls: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Flatten the per-call ``action_trace`` lists from AgentRecorder."""
    actions: List[Dict[str, Any]] = []
    for call in vlm_calls:
        trace = call.get("args", {}).get("action_trace", [])
        actions.extend(trace)
    return actions


def _collect_existing_op_names(app: str, recipe_store: Any) -> List[str]:
    """Return the union of learned + YAML-defined op names for ``app``.

    Used by operation_namer for dedup. Failure is best-effort.
    """
    names: List[str] = []
    try:
        for r in recipe_store.list_recipes():
            op = r.get("operation", "")
            if op.startswith(f"{app}/"):
                names.append(op.split("/", 1)[1])
    except Exception:
        pass

    try:
        import yaml
        from pathlib import Path
        ops_dir = (
            Path(__file__).resolve().parent.parent
            / "config" / "operations"
        )
        for yf in ops_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(yf.read_text(encoding="utf-8")) or {}
                for op in data.get("operations", []):
                    if op.get("affiliated_app") == app:
                        names.append(op.get("name", ""))
            except Exception:
                pass
    except Exception:
        pass

    return [n for n in names if n]


def _name_nl_operation(
    prompt: str,
    actions: List[Dict[str, Any]],
    app: str,
    recipe_store: Any,
) -> Optional[Dict[str, Any]]:
    """Call operation_namer with the given context; return its raw dict
    (or ``None`` on failure). Pure pass-through, no fallback logic.
    """
    from openclaw_agent.engine.learning.operation_namer import name_operation
    existing_names = _collect_existing_op_names(app, recipe_store)
    return name_operation(
        prompt=prompt,
        actions_summary=actions[:5],
        app=app,
        existing_operations=existing_names,
    )


def _resolve_partial_op_name(
    prompt: str,
    actions: List[Dict[str, Any]],
    app: str,
    recipe_store: Any,
) -> str:
    """Resolve an op name for a *failed* NL trace.

    Strategy (in order):
      1. operation_namer says ``should_learn=True`` → use its name
      2. operation_namer declines but the reason mentions an existing
         op verbatim → use that existing op (keeps retry attempts in
         the same file as the eventual success)
      3. naming fails entirely → fall back to ``_unnamed_<promptHash>``

    The hash bucket means repeated attempts at the same prompt still
    aggregate, even when the LLM is unavailable.
    """
    naming = _name_nl_operation(
        prompt=prompt, actions=actions, app=app,
        recipe_store=recipe_store,
    )
    if naming and naming.get("should_learn") and naming.get("name"):
        return naming["name"]

    # LLM declined: try to recover an existing op name from the reason.
    if naming and not naming.get("should_learn"):
        reason = naming.get("reason", "") or ""
        if reason:
            for existing in _collect_existing_op_names(app, recipe_store):
                if existing and existing in reason:
                    logger.info(
                        f"NL-partial: LLM flagged duplicate of "
                        f"'{existing}', filing under it")
                    return existing

    # Total fallback — stable per-prompt key.
    import hashlib
    h = hashlib.sha1(prompt.strip().encode("utf-8")).hexdigest()[:8]
    return f"_unnamed_{h}"
