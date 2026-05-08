"""AdaptiveStep — two-tier self-evolving RPA framework.

The original u2 RPA code (``rpa_fn``) is the seed recipe (v0).
When a learned recipe exists in RecipeStore, it replaces rpa_fn.
If the recipe fails, VLM takes over from where the recipe left off.

Execution:
  0. Pre-check — skip if already on target page (via AppGraph)
  1. Recipe  (learned recipe if available, otherwise rpa_fn as v0)
  2. VLM fallback (completes the remaining work)

After VLM succeeds, the combined trace is saved to TraceStore,
and the app page graph is updated — including discovering new
states via vision LLM when the page is unknown.

See DESIGN.md for the full architecture.
"""

import threading
import time
from typing import Any, Callable

from openclaw_agent.engine.common.logger import get_logger
from openclaw_agent.engine.learning.event_log import emit as emit_event
from openclaw_agent.engine.learning.recipe_store import RecipeStore
from openclaw_agent.engine.learning.trace_store import TraceStore

_RECIPE_TIMEOUT = 45  # seconds — recipes should finish well within this
_CANDIDATE_EVAL_RUNS = 3  # runs of each variant before promote/rollback

logger = get_logger("adaptive_step")

_recipe_store: RecipeStore | None = None
_trace_store: TraceStore | None = None

# Per-step locks to prevent concurrent _post_run_async from racing
# on candidate evaluation/review for the same (operation, step).
_post_run_locks: dict[str, threading.Lock] = {}
_post_run_locks_guard = threading.Lock()


def _get_post_run_lock(key: str) -> threading.Lock:
    with _post_run_locks_guard:
        if key not in _post_run_locks:
            _post_run_locks[key] = threading.Lock()
        return _post_run_locks[key]


def _get_recipe_store() -> RecipeStore:
    global _recipe_store
    if _recipe_store is None:
        _recipe_store = RecipeStore()
    return _recipe_store


def _get_trace_store() -> TraceStore:
    global _trace_store
    if _trace_store is None:
        _trace_store = TraceStore()
    return _trace_store


class AdaptiveStep:
    """A single named step that starts with a seed RPA recipe and
    evolves via VLM fallback traces.

    Usage::

        step = AdaptiveStep("douyin/enter_data_center", "go_to_me_tab",
                            target_page="profile")
        nav = []
        ok = step.run(
            device, agent,
            rpa_fn=lambda: self._try_click_me_tab(device),
            vlm_prompt="点击底部导航栏「我」tab。完成后回复「完成」。",
            verify_fn=lambda: self._verify_me_page(device),
            nav_path=nav,
        )
    """

    def __init__(self, operation: str, step: str,
                 target_page: str | None = None):
        self.operation = operation
        self.step = step
        self.target_page = target_page

    @property
    def _app(self) -> str:
        return self.operation.split("/")[0] if "/" in self.operation else ""

    def run(
        self,
        device: Any,
        agent: Any,
        rpa_fn: Callable[[], bool],
        vlm_prompt: str,
        verify_fn: Callable[[], bool] | None = None,
        device_id: str = "",
        nav_path: list[str] | None = None,
    ) -> bool:
        """Execute with two-tier fallback and optional page pre-check.

        **Pre-check** (when ``target_page`` is set):
            Uses the app page state graph to detect current page.
            If already on the target page, returns True immediately
            without executing anything.

        Tier 1 — **Recipe** (learned or candidate, alternating when
        a candidate exists; else seed rpa_fn).
        Tier 2 — **VLM fallback**.

        After execution, triggers an async LLM review that decides
        whether to generate/promote/rollback candidate recipes.

        Returns:
            True when either tier succeeds (and passes verification).
        """
        tag = f"{self.operation}/{self.step}"
        recipe_store = _get_recipe_store()

        _ev = dict(operation=self.operation, step=self.step, device_id=device_id)
        emit_event("step.started", {
            "has_recipe": bool(recipe_store.get(self.operation, self.step)),
            "target_page": self.target_page,
        }, **_ev)

        # -- Pre-check: skip if already on target page --------------------
        if self.target_page:
            try:
                current = _detect_page(device, self.operation)
                if current == self.target_page:
                    logger.info(
                        f"[{tag}] pre-check: already on '{self.target_page}'"
                        ", skipping")
                    if nav_path is not None:
                        nav_path.append(self.target_page)
                    emit_event("step.completed", {
                        "success": True, "tier_used": "precheck",
                    }, **_ev)
                    return True
            except Exception:
                logger.debug(f"[{tag}] pre-check failed, continuing")

        # Exploration mode: ensure nav_path has a starting page so
        # _update_graph can record transition edges and discovery_path.
        if nav_path is not None and not nav_path and not self.target_page:
            try:
                start = _detect_page(device, self.operation)
                if not start:
                    time.sleep(1.0)
                    start = _detect_page(device, self.operation)
                if start:
                    nav_path.append(start)
                    logger.info(
                        f"[{tag}] exploration: auto-detected start "
                        f"page '{start}'")
                else:
                    logger.debug(
                        f"[{tag}] exploration: no start page detected")
            except Exception:
                pass

        record_offset = _get_record_offset(device)

        # -- Tier 1: recipe (candidate / learned / seed rpa_fn) -----------
        used_vlm = False
        variant = "current"
        recipe = recipe_store.get(self.operation, self.step)

        # Candidate alternation: if a candidate exists, alternate
        # between current and candidate based on total_runs parity
        candidate = None
        if recipe and recipe_store.has_candidate(self.operation, self.step):
            candidate = recipe_store.get_candidate(self.operation, self.step)
            if candidate:
                cur_meta = recipe_store.read_meta(
                    self.operation, self.step)
                cand_meta = recipe_store.read_meta(
                    self.operation, self.step, variant="candidate")
                cur_runs = cur_meta.get("total_runs", 0)
                cand_runs = cand_meta.get("total_runs", 0)
                # Use whichever has fewer runs to keep counts balanced
                if cand_runs <= cur_runs:
                    recipe = candidate
                    variant = "candidate"
                    logger.info(
                        f"[{tag}] using CANDIDATE recipe "
                        f"(cur={cur_runs} cand={cand_runs})")
                else:
                    logger.info(
                        f"[{tag}] using CURRENT recipe "
                        f"(cur={cur_runs} cand={cand_runs})")

        t0 = time.monotonic()

        if recipe:
            label = "candidate" if variant == "candidate" else "learned"
            logger.info(f"[{tag}] recipe: trying {label} recipe "
                        f"({recipe.path.name})")
            # Merge verification: external verify_fn > recipe.verify > None
            effective_verify = verify_fn or (
                (lambda dev=device: recipe.verify(dev))
                if recipe.verify else None)
            try:
                ok = _run_with_timeout(
                    lambda: recipe.execute(device),
                    _RECIPE_TIMEOUT, tag, f"{label} recipe")
                duration_ms = (time.monotonic() - t0) * 1000
                logger.info(f"[{tag}] recipe: {label} returned {ok} "
                            f"in {duration_ms:.0f}ms")
                if ok:
                    # Brief wait for page elements to finish rendering
                    # before running verify — avoids false negatives
                    # from detect_page on freshly-loaded pages
                    time.sleep(1.5)
                if ok and (effective_verify is None or effective_verify()):
                    recipe_store.record_result(
                        self.operation, self.step, True,
                        duration_ms=duration_ms, variant=variant)
                    logger.info(f"[{tag}] recipe: {label} succeeded")
                    emit_event("step.recipe_executed", {
                        "variant": variant, "success": True,
                        "duration_ms": int(duration_ms),
                    }, **_ev)
                    if nav_path is not None and self.target_page:
                        nav_path.append(self.target_page)
                    # Exploration tasks: run page discovery even on recipe
                    # success, since the whole point is to map new pages
                    if not self.target_page:
                        self._update_graph(device, nav_path)
                    self._post_run_async(variant, used_vlm=False)
                    emit_event("step.completed", {
                        "success": True, "tier_used": "recipe",
                        "duration_ms": int(duration_ms),
                    }, **_ev)
                    return True
                # Recipe failed — don't record yet; wait for VLM outcome
                # to avoid double-counting total_runs per run
                logger.warning(f"[{tag}] recipe: {label} failed (ok={ok})")
                emit_event("step.recipe_executed", {
                    "variant": variant, "success": False,
                    "duration_ms": int(duration_ms),
                }, **_ev)
            except TimeoutError:
                logger.error(f"[{tag}] recipe: {label} TIMED OUT "
                             f"after {_RECIPE_TIMEOUT}s")
            except Exception:
                logger.exception(f"[{tag}] recipe: {label} raised")
        else:
            logger.info(f"[{tag}] recipe: trying seed RPA (v0)")
            try:
                ok = _run_with_timeout(
                    rpa_fn, _RECIPE_TIMEOUT, tag, "seed RPA")
                duration_ms = (time.monotonic() - t0) * 1000
                logger.info(f"[{tag}] recipe: seed RPA returned {ok} "
                            f"in {duration_ms:.0f}ms")
                if ok:
                    if verify_fn is None or verify_fn():
                        logger.info(f"[{tag}] recipe: seed RPA succeeded")
                        emit_event("step.recipe_executed", {
                            "variant": "seed", "success": True,
                            "duration_ms": int(duration_ms),
                        }, **_ev)
                        if nav_path is not None and self.target_page:
                            nav_path.append(self.target_page)
                        if not self.target_page:
                            self._update_graph(device, nav_path)
                        emit_event("step.completed", {
                            "success": True, "tier_used": "recipe",
                            "duration_ms": int(duration_ms),
                        }, **_ev)
                        return True
                    logger.warning(
                        f"[{tag}] recipe: seed RPA passed but verify failed")
                emit_event("step.recipe_executed", {
                    "variant": "seed", "success": False,
                    "duration_ms": int(duration_ms),
                }, **_ev)
            except TimeoutError:
                logger.error(f"[{tag}] recipe: seed RPA TIMED OUT "
                             f"after {_RECIPE_TIMEOUT}s")
            except Exception:
                logger.exception(f"[{tag}] recipe: seed RPA raised")

        # -- Tier 2: VLM fallback -----------------------------------------
        used_vlm = True
        logger.info(f"[{tag}] vlm: fallback from recipe failure")
        emit_event("step.vlm_started", {
            "prompt_preview": vlm_prompt[:120],
        }, **_ev)

        pre_vlm_hierarchy: str | None = None
        try:
            raw_dev = _unwrap_device(device)
            pre_vlm_hierarchy = raw_dev.dump_hierarchy()
        except Exception:
            logger.debug(f"[{tag}] vlm: dump_hierarchy before VLM failed")

        failure_outcome: str | None = None
        try:
            logger.info(f"[{tag}] vlm: calling agent.run()")
            agent.run(vlm_prompt)
            logger.info(f"[{tag}] vlm: agent.run() returned, sleeping 1s")
            time.sleep(1.0)

            logger.info(f"[{tag}] vlm: running verify_fn")
            if verify_fn is None or verify_fn():
                vlm_duration_ms = (time.monotonic() - t0) * 1000
                logger.info(f"[{tag}] vlm: verify passed")
                # Count VLM actions for event payload
                _vlm_actions_count = 0
                try:
                    _vlm_actions_count = len(_unwrap_agent(agent).last_action_trace)
                except Exception:
                    pass
                emit_event("step.vlm_completed", {
                    "success": True,
                    "actions_count": _vlm_actions_count,
                    "duration_ms": int(vlm_duration_ms),
                }, **_ev)
                if nav_path is not None and self.target_page:
                    nav_path.append(self.target_page)
                # Record VLM fallback in the variant that failed
                recipe_store.record_result(
                    self.operation, self.step, True,
                    duration_ms=vlm_duration_ms,
                    vlm_fallback=True, variant=variant)
                logger.info(f"[{tag}] vlm: calling _save_trace")
                self._save_trace(
                    agent, device, device_id, record_offset, nav_path,
                    pre_vlm_hierarchy)
                logger.info(f"[{tag}] vlm: _save_trace done")
                self._post_run_async(variant, used_vlm=True)
                emit_event("step.completed", {
                    "success": True, "tier_used": "vlm",
                    "duration_ms": int(vlm_duration_ms),
                }, **_ev)
                return True

            logger.warning(f"[{tag}] vlm: finished but verify failed")
            failure_outcome = "verify_failed"
            emit_event("step.vlm_completed", {
                "success": False, "duration_ms": int((time.monotonic() - t0) * 1000),
            }, **_ev)
        except Exception:
            logger.exception(f"[{tag}] vlm: raised")
            failure_outcome = "vlm_error"

        # Both recipe and VLM failed — record a single failure
        final_duration_ms = (time.monotonic() - t0) * 1000
        recipe_store.record_result(
            self.operation, self.step, False,
            duration_ms=final_duration_ms, vlm_fallback=used_vlm,
            variant=variant)
        logger.error(f"[{tag}] all tiers failed")
        # Persist whatever VLM and recipe-stage actions we collected so
        # the failure is auditable / RL-trainable. Don't update the
        # graph or trigger recipe gen on failure.
        try:
            self._persist_trace_record(
                agent, device, device_id, record_offset,
                pre_vlm_hierarchy,
                outcome=failure_outcome or "all_tiers_failed",
            )
        except Exception:
            logger.exception(f"[{tag}] failed to persist failure trace")
        emit_event("step.completed", {
            "success": False, "tier_used": "vlm" if used_vlm else "recipe",
            "duration_ms": int(final_duration_ms),
        }, **_ev)
        self._post_run_async(variant, used_vlm=True)

        if self.target_page and self._app:
            try:
                from openclaw_agent.engine.learning.app_graph import get_app_graph
                get_app_graph(self._app).record_match_failure(self.target_page)
            except Exception:
                pass

        return False

    # -- internals ---------------------------------------------------------

    def _persist_trace_record(
        self, agent: Any, device: Any,
        device_id: str, record_offset: int,
        pre_vlm_hierarchy: str | None,
        outcome: str,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """Persist the combined trace (VLM + RPA) under the given outcome.

        Shared by the success path (``_save_trace``) and the failure
        branch. Returns ``(persisted, vlm_trace)``: ``persisted`` is
        False only when both VLM and RPA streams were empty.
        """
        tag = f"{self.operation}/{self.step}"
        vlm_trace: list[dict[str, Any]] = []
        try:
            raw_agent = _unwrap_agent(agent)
            vlm_trace = raw_agent.last_action_trace
        except AttributeError:
            pass
        logger.info(
            f"[{tag}] persist_trace ({outcome}): "
            f"{len(vlm_trace)} vlm actions")

        # Enrich Tap actions with element_region from hierarchy
        if pre_vlm_hierarchy and vlm_trace:
            _enrich_trace_with_bounds(vlm_trace, pre_vlm_hierarchy)

        rpa_commands: list[dict[str, Any]] = []
        try:
            all_records = object.__getattribute__(device, "_records")
            rpa_commands = [
                r.to_dict() for r in all_records[record_offset:]
                if not r.is_vlm
            ]
        except (AttributeError, TypeError):
            pass
        logger.info(
            f"[{tag}] persist_trace ({outcome}): "
            f"{len(rpa_commands)} rpa commands")

        if not vlm_trace and not rpa_commands:
            logger.info(
                f"[{tag}] persist_trace ({outcome}): no data, skipping")
            return False, vlm_trace

        from openclaw_agent.engine.common import get_task_id
        current_task_id = get_task_id() or None
        _get_trace_store().save(
            operation=self.operation,
            step=self.step,
            trace=vlm_trace,
            device_id=device_id,
            extra={
                "rpa_commands_before_fallback": rpa_commands,
            } if rpa_commands else None,
            task_id=current_task_id,
            outcome=outcome,
        )
        logger.info(f"[{tag}] persist_trace ({outcome}): done")
        return True, vlm_trace

    def _save_trace(
        self, agent: Any, device: Any,
        device_id: str, record_offset: int,
        nav_path: list[str] | None,
        pre_vlm_hierarchy: str | None = None,
    ) -> None:
        """Save the combined trace and run success-only follow-ups
        (graph update, recipe-gen trigger).

        When *pre_vlm_hierarchy* is provided, each VLM Tap action is
        enriched with ``element_region`` — the actual element bounds
        from the UI hierarchy, matched by proximity to the VLM's click
        coordinates.
        """
        tag = f"{self.operation}/{self.step}"
        logger.info(f"[{tag}] _save_trace: start")
        persisted, vlm_trace = self._persist_trace_record(
            agent, device, device_id, record_offset,
            pre_vlm_hierarchy, outcome="completed",
        )
        if not persisted:
            return

        # Extract VLM finish message for page discovery context
        vlm_finish_msg = ""
        if vlm_trace:
            last_action = vlm_trace[-1]
            if last_action.get("action_type") == "finish":
                vlm_finish_msg = last_action.get("message", "")

        logger.info(f"[{tag}] _save_trace: calling _update_graph")
        self._update_graph(device, nav_path,
                           vlm_finish_message=vlm_finish_msg)
        logger.info(f"[{tag}] _save_trace: _update_graph done")

        # VLM fallback succeeded → async trigger recipe generation
        # (kept for initial recipe creation when no learned recipe exists)
        if not _get_recipe_store().get(self.operation, self.step):
            self._trigger_recipe_gen_async()

    def _post_run_async(self, variant: str, used_vlm: bool) -> None:
        """Fire-and-forget post-run tasks: candidate evaluation + LLM review.

        Uses a per-step lock so only one thread at a time can evaluate or
        review a given step — prevents racing threads from generating
        duplicate candidates.
        """
        op, step = self.operation, self.step
        tag = f"{op}/{step}"

        def _work() -> None:
            lock = _get_post_run_lock(f"{op}/{step}")
            if not lock.acquire(blocking=False):
                logger.debug(
                    f"[{tag}] post-run: another review in progress, skipping")
                return
            try:
                store = _get_recipe_store()
                # --- Evaluate candidate if enough runs collected ---
                if store.has_candidate(op, step):
                    self._evaluate_candidate(store, op, step)

                # --- LLM review: decide whether to generate new candidate ---
                from openclaw_agent.engine.learning.recipe_reviewer import (
                    RecipeReviewer,
                )
                reviewer = RecipeReviewer(
                    recipe_store=store, trace_store=_get_trace_store())
                if reviewer.should_review(op, step):
                    result = reviewer.review_and_maybe_improve(op, step)
                    logger.info(
                        f"[{tag}] review: verdict={result.get('verdict')} "
                        f"candidate_generated="
                        f"{result.get('candidate_generated')}")
            except Exception:
                logger.opt(exception=True).warning(
                    f"[{tag}] post-run async failed")
            finally:
                lock.release()

        t = threading.Thread(
            target=_work, daemon=True, name=f"post-run-{step}")
        t.start()
        logger.debug(f"[{tag}] post-run async triggered")

    def _evaluate_candidate(
        self, store: RecipeStore, op: str, step: str,
    ) -> None:
        """Compare candidate vs current after both have enough runs.

        Promote if candidate success rate >= current AND VLM fallback
        rate <= current.  Duration is tracked but not used as a gate
        (reliability matters more than speed during recipe evolution).
        """
        cur = store.read_meta(op, step)
        cand = store.read_meta(op, step, variant="candidate")
        cur_runs = cur.get("total_runs", 0)
        cand_runs = cand.get("total_runs", 0)

        if cand_runs < _CANDIDATE_EVAL_RUNS:
            return

        tag = f"{op}/{step}"
        cur_sr = cur.get("success", 0) / max(cur_runs, 1)
        cand_sr = cand.get("success", 0) / max(cand_runs, 1)
        cur_vlm = cur.get("vlm_fallback_count", 0) / max(cur_runs, 1)
        cand_vlm = cand.get("vlm_fallback_count", 0) / max(cand_runs, 1)

        logger.info(
            f"[{tag}] candidate eval: "
            f"cur(sr={cur_sr:.0%} vlm={cur_vlm:.0%} n={cur_runs}) "
            f"cand(sr={cand_sr:.0%} vlm={cand_vlm:.0%} n={cand_runs})")

        # Promote if candidate is strictly better or equal on success
        # and has fewer VLM fallbacks
        if cand_sr >= cur_sr and cand_vlm <= cur_vlm:
            store.promote(op, step)
            logger.info(f"[{tag}] candidate PROMOTED")
            emit_event("recipe.candidate_promoted", {
                "candidate_rate": round(cand_sr, 3),
                "current_rate": round(cur_sr, 3),
                "candidate_vlm_rate": round(cand_vlm, 3),
                "current_vlm_rate": round(cur_vlm, 3),
            }, operation=op, step=step)
        else:
            reason = (f"cur_sr={cur_sr:.0%} cand_sr={cand_sr:.0%} "
                      f"cur_vlm={cur_vlm:.0%} cand_vlm={cand_vlm:.0%}")
            store.rollback(op, step, reason=reason)
            logger.info(f"[{tag}] candidate ROLLED BACK")
            emit_event("recipe.candidate_rolled_back", {
                "reason": reason,
            }, operation=op, step=step)

    def _trigger_recipe_gen_async(self) -> None:
        """Fire-and-forget: generate initial recipe from VLM trace.

        Only used when no learned recipe exists yet (bootstrap).
        """
        op, step = self.operation, self.step
        tag = f"{op}/{step}"

        def _gen() -> None:
            try:
                from openclaw_agent.engine.learning.recipe_generator import (
                    RecipeGenerator,
                )
                gen = RecipeGenerator()
                count = gen.process_for_step(op, step)
                if count:
                    logger.info(
                        f"[{tag}] async recipe gen produced {count} recipe(s)")
            except Exception:
                logger.opt(exception=True).warning(
                    f"[{tag}] async recipe gen failed")

        t = threading.Thread(
            target=_gen, daemon=True, name=f"recipe-gen-{step}")
        t.start()
        logger.info(f"[{tag}] async recipe gen triggered")

    def _update_graph(
        self, device: Any, nav_path: list[str] | None,
        vlm_finish_message: str = "",
    ) -> None:
        """Update the persistent app page graph after VLM succeeds.

        Runs best-effort after every VLM success — no target_page required.
        When target_page is set, fast-path cases 1-2 skip detect_page.

        **Exploration mode** (target_page is None): bypasses detect_page
        entirely and goes straight to VLM-based discover_state, since
        text-based indicator matching is unreliable for unknown pages.
        """
        app = self._app
        if not app:
            return
        tag = f"{self.operation}/{self.step}"
        target = self.target_page
        logger.info(
            f"[{tag}] _update_graph: start (target={target})")
        try:
            from openclaw_agent.engine.learning.app_graph import get_app_graph
            graph = get_app_graph(app)

            prior_page = None
            if nav_path:
                if target:
                    # Target mode: target_page was appended to nav_path
                    # before this call, so [-2] is the prior page.
                    if len(nav_path) >= 2:
                        prior_page = nav_path[-2]
                else:
                    # Exploration mode: target_page is never appended,
                    # so [-1] is the starting page (= prior page before
                    # whatever new page we may have reached).
                    prior_page = nav_path[-1]
            logger.info(f"[{tag}] _update_graph: prior={prior_page}")

            # Fast path: only when target_page is set and known
            if target:
                # Case 1: known transition edge → just record visit
                if prior_page and graph.has_transition(prior_page, target):
                    graph.record_page_visit(target)
                    emit_event("page.visited", {
                        "app": self.operation.split("/")[0] if "/" in self.operation else "",
                        "state_id": target, "via": "transition",
                    }, operation=self.operation, step=self.step)
                    logger.info(f"[{tag}] _update_graph: case1 done")
                    return

                # Case 2: target_page is a known state → record directly
                pages = graph.get_pages()
                if target in pages:
                    graph.record_page_visit(target)
                    emit_event("page.visited", {
                        "app": self.operation.split("/")[0] if "/" in self.operation else "",
                        "state_id": target, "via": "known_state",
                    }, operation=self.operation, step=self.step)
                    if prior_page and not graph.has_transition(
                            prior_page, target):
                        graph.record_transition(
                            prior_page, target,
                            hint=f"通过 {self.step} 步骤到达")
                    logger.info(f"[{tag}] _update_graph: case2 done")
                    return

            # Exploration mode: no target_page → skip detect_page,
            # use hash pre-check then VLM-based discovery
            if not target:
                logger.info(
                    f"[{tag}] _update_graph: exploration mode, "
                    "bypassing detect_page")
                # Hash pre-check: if visually similar to a known page,
                # skip VLM discovery call
                try:
                    from openclaw_agent.engine.learning.screenshot_hash import (
                        compute_phash,
                    )
                    current_hash = compute_phash(device)
                    best_match, distance = (
                        graph.find_closest_page_by_hash(current_hash))
                    logger.info(
                        f"[{tag}] _update_graph: hash check "
                        f"best={best_match} dist={distance}")
                    if best_match and distance < 12:
                        # dist ≤ 3: near-identical screenshot (only 3 of
                        # 64 bits differ), false positive risk negligible
                        confirmed = (distance <= 3
                                     or _verify_hash_by_indicators(
                                         device, graph, best_match))
                        if confirmed:
                            graph.record_page_visit(best_match)
                            graph.add_screenshot_hash(
                                best_match, current_hash)
                            emit_event("page.visited", {
                                "app": self.operation.split("/")[0] if "/" in self.operation else "",
                                "state_id": best_match, "via": "hash",
                            }, operation=self.operation, step=self.step)
                            if prior_page and not graph.has_transition(
                                    prior_page, best_match):
                                graph.record_transition(
                                    prior_page, best_match,
                                    hint=f"通过 {self.step} 步骤到达")
                            logger.info(
                                f"[{tag}] _update_graph: hash matched "
                                f"'{best_match}' (dist={distance}), "
                                "skipping VLM")
                            return
                        logger.info(
                            f"[{tag}] _update_graph: hash candidate "
                            f"'{best_match}' (dist={distance}) but "
                            "indicator check failed, using VLM")
                except Exception:
                    logger.debug(
                        f"[{tag}] _update_graph: hash check failed, "
                        "falling through to VLM")

                self._defer_discovery(
                    device, graph, nav_path, prior_page)
                logger.info(f"[{tag}] _update_graph: discovery deferred")
                return

            # Known-target path: detect current page via text matching
            logger.info(f"[{tag}] _update_graph: detect_page start")
            detected = graph.detect_page(device, timeout=0.8)
            logger.info(
                f"[{tag}] _update_graph: detect_page "
                f"result={detected}")
            if detected:
                graph.record_page_visit(detected)
                emit_event("page.visited", {
                    "app": self.operation.split("/")[0] if "/" in self.operation else "",
                    "state_id": detected, "via": "detect_page",
                }, operation=self.operation, step=self.step)
                if prior_page and not graph.has_transition(
                        prior_page, detected):
                    graph.record_transition(
                        prior_page, detected,
                        hint=f"通过 {self.step} 步骤到达")
                # Accumulate screenshot hash for confirmed page visits
                self._accumulate_hash(device, graph, detected, tag)
                logger.info(
                    f"[{tag}] _update_graph: matched '{detected}'")
                return

            # Unknown page → defer VLM discovery to background
            logger.info(f"[{tag}] _update_graph: deferring discovery")
            self._defer_discovery(
                device, graph, nav_path, prior_page)
            logger.info(f"[{tag}] _update_graph: discovery deferred")

        except Exception:
            logger.opt(exception=True).warning(
                f"[{tag}] Failed to update app graph")

    @staticmethod
    def _accumulate_hash(
        device: Any, graph: Any, state_id: str, tag: str,
    ) -> None:
        """Compute and store a perceptual hash for a confirmed page visit."""
        try:
            from openclaw_agent.engine.learning.screenshot_hash import (
                compute_phash,
            )
            h = compute_phash(device)
            graph.add_screenshot_hash(state_id, h)
            logger.debug(f"[{tag}] accumulated hash for '{state_id}'")
        except Exception:
            logger.debug(f"[{tag}] hash accumulation failed")

    def _defer_discovery(
        self,
        device: Any,
        graph: Any,
        nav_path: list[str] | None,
        prior_page: str | None,
    ) -> None:
        """Capture screenshot now, persist to JSONL for async VLM discovery."""
        tag = f"{self.operation}/{self.step}"
        try:
            from openclaw_agent.engine.learning.screenshot_hash import (
                compute_phash,
            )
            from openclaw_agent.engine.learning.vision_utils import (
                take_screenshot_b64,
            )
            from openclaw_agent.engine.learning.graph_updater import (
                PendingDiscoveryWriter,
            )
            import uuid
            from datetime import datetime, timezone

            screenshot_b64 = take_screenshot_b64(device)
            phash = compute_phash(device)
            app = self._app or ""

            writer = PendingDiscoveryWriter(app)
            writer.append({
                "id": uuid.uuid4().hex[:12],
                "app": app,
                "screenshot_b64": screenshot_b64,
                "phash": phash,
                "nav_path": list(nav_path) if nav_path else [],
                "device_id": getattr(device, "serial", ""),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "consumed": False,
            })
            logger.info(
                f"[{tag}] deferred discovery: captured screenshot "
                f"(phash={phash[:8]}...)")
        except Exception:
            logger.debug(
                f"[{tag}] deferred discovery capture failed",
                exc_info=True)

    def _discover_new_state(
        self,
        device: Any,
        graph: Any,
        nav_path: list[str] | None,
        prior_page: str | None,
        vlm_context: str = "",
    ) -> None:
        """Run the state discovery algorithm for an unknown page."""
        try:
            from openclaw_agent.engine.learning.state_discovery import (
                check_stability,
                discover_state,
            )
        except ImportError:
            logger.debug("state_discovery module not available")
            return

        tag = f"{self.operation}/{self.step}"

        if not check_stability(device):
            # May be a video feed page — try tapping center to pause video,
            # then recheck stability before giving up
            try:
                raw_dev = _unwrap_device(device)
                w, h = raw_dev.window_size()
                raw_dev.click(w // 2, h // 2)
                time.sleep(0.5)
            except Exception:
                pass
            if not check_stability(device, wait_seconds=1.0):
                logger.info(
                    f"[{tag}] graph: page is transient after retry, "
                    "skipping discovery")
                return
            logger.info(
                f"[{tag}] graph: page stabilized after tap-to-pause")

        result = discover_state(
            device, graph, nav_path=nav_path or [],
            vlm_context=vlm_context)
        if not result:
            logger.warning(
                f"[{tag}] graph: state discovery returned None")
            return

        if result.is_new:
            graph.register_state(
                state_id=result.state_id,
                name=result.name,
                description=result.description,
                indicators=result.indicators,
                discovery_path=list(nav_path) if nav_path else [],
                is_optional=result.is_optional,
                from_page=prior_page,
            )
            logger.info(
                f"[{tag}] graph: discovered new state "
                f"'{result.state_id}' ({result.name})")
            app = self.operation.split("/")[0] if "/" in self.operation else ""
            emit_event("page.discovered", {
                "app": app,
                "state_id": result.state_id,
                "name": result.name,
                "from_page": prior_page or "",
                "discovery_path": list(nav_path) if nav_path else [],
            }, operation=self.operation, step=self.step)
        else:
            graph.record_page_visit(result.state_id)
            if prior_page and not graph.has_transition(
                    prior_page, result.state_id):
                graph.record_transition(
                    prior_page, result.state_id,
                    hint=f"通过 {self.step} 步骤到达")
            logger.info(
                f"[{tag}] graph: vision matched existing "
                f"'{result.state_id}'")
            emit_event("page.visited", {
                "app": self.operation.split("/")[0] if "/" in self.operation else "",
                "state_id": result.state_id,
                "via": "vlm",
            }, operation=self.operation, step=self.step)

        # Store perceptual hash for the confirmed page
        self._accumulate_hash(device, graph, result.state_id, tag)


def _verify_hash_by_indicators(
    device: Any, graph: Any, state_id: str,
) -> bool:
    """Quick text-indicator check to confirm a hash match.

    Hash matching alone can produce false positives (e.g. two pages with
    large dark areas both hash similarly).  This verifies by checking
    whether the matched page's text indicators actually appear on screen.

    Uses a single dump_hierarchy() call + in-memory string matching
    (same approach as detect_page) instead of per-indicator ADB calls,
    which are slower and less reliable for WebView/custom-rendered text.
    """
    pages = graph.get_pages()
    state = pages.get(state_id)
    if not state or not state.indicators:
        return True  # no indicators to check — trust hash

    raw_dev = _unwrap_device(device)
    try:
        xml = raw_dev.dump_hierarchy()
    except Exception:
        return False

    threshold = getattr(state, "match_threshold", 2)
    hits = 0
    for ind in state.indicators:
        text = ind.get("text", "") if isinstance(ind, dict) else str(ind)
        if not text:
            continue
        if text in xml:
            hits += 1
            if hits >= threshold:
                return True
    return hits >= threshold


def _run_with_timeout(
    fn: Callable[[], Any],
    timeout: float,
    tag: str,
    label: str,
) -> Any:
    """Run *fn* in a daemon thread; raise TimeoutError if it exceeds *timeout*.

    The thread is a daemon so it won't block process exit if it truly hangs.
    """
    result_box: list[Any] = []
    error_box: list[BaseException] = []

    def _worker() -> None:
        try:
            result_box.append(fn())
        except BaseException as exc:
            error_box.append(exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        logger.error(f"[{tag}] {label} still running after {timeout}s, "
                     "treating as timeout")
        raise TimeoutError(f"{label} exceeded {timeout}s")

    if error_box:
        raise error_box[0]

    return result_box[0] if result_box else False


def _unwrap_agent(agent: Any) -> Any:
    """Unwrap AgentRecorder proxy to get the real PhoneAgent."""
    try:
        return object.__getattribute__(agent, "_agent")
    except AttributeError:
        return agent


def _unwrap_device(device: Any) -> Any:
    """Unwrap DeviceRecorder proxy to get the real u2 device."""
    try:
        return object.__getattribute__(device, "_device")
    except AttributeError:
        return device


def _get_record_offset(device: Any) -> int:
    """Get the current record count from DeviceRecorder so we can
    later slice only this step's commands."""
    try:
        return len(object.__getattribute__(device, "_records"))
    except (AttributeError, TypeError):
        return 0


def _detect_page(device: Any, operation: str) -> str | None:
    """Detect the current page using the app page graph."""
    app = operation.split("/")[0] if "/" in operation else ""
    if not app:
        return None
    try:
        from openclaw_agent.engine.learning.app_graph import get_app_graph
        return get_app_graph(app).detect_page(device)
    except Exception:
        return None


def _enrich_trace_with_bounds(
    vlm_trace: list[dict[str, Any]],
    hierarchy_xml: str,
) -> None:
    """Add ``element_region`` to Tap actions by matching VLM click
    coordinates against actual element bounds from the UI hierarchy.

    Only enriches the *first* Tap action (the hierarchy snapshot is
    from before VLM started; subsequent taps may be on different pages).
    Coordinates are stored in **absolute pixels**.
    """
    import re as _re

    # Parse all element bounds from the u2 XML hierarchy.
    # Format: bounds="[left,top][right,bottom]"
    bounds_pattern = _re.compile(
        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
    elements: list[tuple[int, int, int, int]] = []
    for m in bounds_pattern.finditer(hierarchy_xml):
        left, top, right, bottom = (
            int(m.group(1)), int(m.group(2)),
            int(m.group(3)), int(m.group(4)))
        if right > left and bottom > top:
            elements.append((left, top, right, bottom))

    if not elements:
        return

    enriched = False
    for action in vlm_trace:
        if action.get("action_type") != "Tap":
            continue
        if "element_region" in action:
            continue
        elem = action.get("element")
        if not elem or len(elem) < 2:
            continue

        sw = action.get("screen_width", 1080)
        sh = action.get("screen_height", 2400)
        # VLM coords are 0-999 relative; convert to absolute pixels
        abs_x = int(elem[0] / 1000 * sw)
        abs_y = int(elem[1] / 1000 * sh)

        best_bounds = _find_nearest_element(abs_x, abs_y, elements)
        if best_bounds:
            action["element_region"] = list(best_bounds)
            logger.info(
                f"Enriched Tap({elem}) -> element_region={list(best_bounds)}")
            enriched = True
        # Only enrich first Tap (hierarchy is only valid for first page)
        break

    if not enriched:
        logger.debug("No Tap actions enriched with element_region")


def _find_nearest_element(
    x: int, y: int,
    elements: list[tuple[int, int, int, int]],
    max_dist: int = 200,
) -> tuple[int, int, int, int] | None:
    """Find the smallest element whose center is closest to (x, y).

    Prefers elements that *contain* the point; among those, picks the
    smallest by area.  Falls back to nearest-center if none contain it.
    """
    # Elements that contain the point
    containing: list[tuple[int, tuple[int, int, int, int]]] = []
    for b in elements:
        left, top, right, bottom = b
        if left <= x <= right and top <= y <= bottom:
            area = (right - left) * (bottom - top)
            containing.append((area, b))

    if containing:
        containing.sort()
        return containing[0][1]

    # Fallback: nearest center
    best: tuple[float, tuple[int, int, int, int]] | None = None
    for b in elements:
        cx = (b[0] + b[2]) / 2
        cy = (b[1] + b[3]) / 2
        dist = ((cx - x) ** 2 + (cy - y) ** 2) ** 0.5
        if dist < max_dist and (best is None or dist < best[0]):
            best = (dist, b)

    return best[1] if best else None
