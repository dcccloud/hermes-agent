"""Persistent, self-evolving app page state graph.

Each app has a JSON-persisted graph that starts from a seed JSON file
and grows as operations discover new pages and transitions.

Architecture:
  seed graph (JSON)    ──┐
                         ├──► merged runtime graph (hot-reloaded)
  learned graph (JSON)  ─┘

Both files are JSON, so edits take effect immediately without restart.
See DESIGN.md for the full design rationale.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openclaw_agent.engine.common.logger import get_logger

logger = get_logger("app_graph")

_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "app_graphs"
_SEED_DIR = Path(__file__).resolve().parent / "app_pages"


VERIFY_SEEN_THRESHOLD = 3     # seen_count needed to become verified
PRUNE_STALE_DAYS = 14         # auto-prune after this many days unseen
PRUNE_FAILURE_RATIO = 0.7     # auto-disable if failure ratio exceeds this


@dataclass
class AppState:
    """A single page/state in the app's state graph."""

    state_id: str
    name: str = ""
    description: str = ""
    discovery_path: list[str] = field(default_factory=list)
    indicators: list[dict[str, Any]] = field(default_factory=list)
    transitions: dict[str, dict[str, Any]] = field(default_factory=dict)
    is_optional: bool = False
    priority: int = 10
    match_threshold: int = 2
    seen_count: int = 0
    last_seen: float = 0.0
    source: str = "seed"
    # Verification status: "verified" (trusted), "unverified" (new/untested),
    # "disabled" (auto-pruned due to stale/failure). Seed pages are always verified.
    verification: str = "verified"
    # Tracking match failures — when detect_page picks this page but
    # subsequent operations fail, increment this counter.
    match_failures: int = 0
    # Visual fingerprints: perceptual hashes from confirmed visits (max 5)
    screenshot_hashes: list[str] = field(default_factory=list)

    @property
    def is_verified(self) -> bool:
        return self.verification == "verified" or self.source == "seed"

    @property
    def is_disabled(self) -> bool:
        return self.verification == "disabled"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "state_id": self.state_id,
            "name": self.name,
            "description": self.description,
            "discovery_path": self.discovery_path,
            "indicators": self.indicators,
            "transitions": self.transitions,
            "is_optional": self.is_optional,
            "priority": self.priority,
            "match_threshold": self.match_threshold,
            "seen_count": self.seen_count,
            "last_seen": self.last_seen,
            "source": self.source,
            "verification": self.verification,
            "match_failures": self.match_failures,
        }
        if self.screenshot_hashes:
            d["screenshot_hashes"] = self.screenshot_hashes
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AppState:
        sid = d.get("state_id") or d.get("page_id", "")
        return cls(
            state_id=sid,
            name=d.get("name", ""),
            description=d.get("description", ""),
            discovery_path=d.get("discovery_path", []),
            indicators=d.get("indicators", []),
            transitions=d.get("transitions", {}),
            is_optional=d.get("is_optional", False),
            priority=d.get("priority", 10),
            match_threshold=d.get("match_threshold", 2),
            seen_count=d.get("seen_count", 0),
            last_seen=d.get("last_seen", 0.0),
            source=d.get("source", "discovered"),
            verification=d.get("verification", "unverified"),
            match_failures=d.get("match_failures", 0),
            screenshot_hashes=d.get("screenshot_hashes", []),
        )


class AppGraph:
    """Persistent, self-evolving page state graph for an app.

    Merges a seed graph (hardcoded) with a learned graph (JSON file).
    The learned graph is updated as operations discover new information.
    """

    def __init__(self, app: str, data_dir: str | None = None):
        self.app = app
        self.initial_state = ""
        effective_dir = Path(data_dir) if data_dir else _DATA_DIR
        self._path = effective_dir / f"{app}.json"
        self._seed_path = _SEED_DIR / f"{app}.seed.json"
        self._seed: dict[str, AppState] = {}
        self._learned: dict[str, AppState] = {}
        self._seed_mtime: float = 0.0
        self._learned_mtime: float = 0.0

        self._load_seed()
        self._load_learned()

    # ── Persistence ──────────────────────────────────────────────────

    def _load_seed(self) -> None:
        """Load seed page definitions from JSON file."""
        if not self._seed_path.exists():
            logger.debug(f"No seed file for {self.app}: {self._seed_path}")
            return
        try:
            mtime = self._seed_path.stat().st_mtime
            self._seed_mtime = mtime
            data = json.loads(self._seed_path.read_text(encoding="utf-8"))
            self.initial_state = data.get("initial_state", "")
            self._seed = {}
            for pid, info in data.get("pages", {}).items():
                indicators = [
                    {"text": t, "type": "textContains", "source": "seed"}
                    for t in info.get("indicators", [])
                ]
                transitions = {}
                for target, hint in info.get("transitions", {}).items():
                    transitions[target] = {
                        "hint": hint,
                        "source": "seed",
                        "seen_count": 0,
                    }
                self._seed[pid] = AppState(
                    state_id=pid,
                    name=info.get("name", pid),
                    description=info.get("description", ""),
                    discovery_path=info.get("discovery_path", []),
                    indicators=indicators,
                    transitions=transitions,
                    is_optional=info.get("is_optional", False),
                    priority=info.get("priority", 10),
                    match_threshold=info.get("match_threshold", 2),
                    source="seed",
                )
            logger.debug(f"Loaded {len(self._seed)} seed states for {self.app}")
        except Exception:
            logger.exception(f"Failed to load seed graph for {self.app}")

    def _load_learned(self) -> None:
        if not self._path.exists():
            return
        try:
            mtime = self._path.stat().st_mtime
            self._learned_mtime = mtime
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._learned = {}
            for d in data.get("pages", []):
                state = AppState.from_dict(d)
                self._learned[state.state_id] = state
            logger.debug(f"Loaded {len(self._learned)} learned states for {self.app}")
        except Exception:
            logger.exception(f"Failed to load app graph for {self.app}")

    def _check_reload(self) -> None:
        """Reload seed/learned files if they changed on disk."""
        try:
            if self._seed_path.exists():
                mt = self._seed_path.stat().st_mtime
                if mt != self._seed_mtime:
                    logger.info(f"Seed file changed, reloading {self.app}")
                    self._load_seed()
            if self._path.exists():
                mt = self._path.stat().st_mtime
                if mt != self._learned_mtime:
                    logger.info(f"Learned file changed, reloading {self.app}")
                    self._load_learned()
        except Exception:
            pass

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "app": self.app,
            "initial_state": self.initial_state,
            "updated_at": time.time(),
            "pages": [s.to_dict() for s in self._learned.values()],
        }
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Update mtime so _check_reload won't re-read our own write
        self._learned_mtime = self._path.stat().st_mtime

    # ── Merged view ──────────────────────────────────────────────────

    def get_pages(self) -> dict[str, AppState]:
        """Return the merged graph: seed + learned (learned wins on conflict).

        Automatically reloads from disk if files changed (hot-reload).
        """
        self._check_reload()
        merged: dict[str, AppState] = {}
        for pid, node in self._seed.items():
            merged[pid] = AppState(
                state_id=pid,
                name=node.name,
                description=node.description,
                discovery_path=list(node.discovery_path),
                indicators=list(node.indicators),
                transitions=dict(node.transitions),
                is_optional=node.is_optional,
                priority=node.priority,
                match_threshold=node.match_threshold,
                seen_count=node.seen_count,
                last_seen=node.last_seen,
                source=node.source,
            )
        for pid, learned in self._learned.items():
            if pid in merged:
                base = merged[pid]
                if learned.name:
                    base.name = learned.name
                if learned.description:
                    base.description = learned.description
                if learned.discovery_path:
                    base.discovery_path = learned.discovery_path
                existing_texts = {i["text"] for i in base.indicators}
                for ind in learned.indicators:
                    if ind["text"] not in existing_texts:
                        base.indicators.append(ind)
                        existing_texts.add(ind["text"])
                base.transitions.update(learned.transitions)
                base.seen_count = max(base.seen_count, learned.seen_count)
                base.last_seen = max(base.last_seen, learned.last_seen)
                if learned.priority > base.priority:
                    base.priority = learned.priority
                if learned.is_optional:
                    base.is_optional = True
                if learned.screenshot_hashes:
                    existing_hashes = set(base.screenshot_hashes)
                    for h in learned.screenshot_hashes:
                        if h not in existing_hashes:
                            base.screenshot_hashes.append(h)
                            existing_hashes.add(h)
            else:
                merged[pid] = AppState(**learned.to_dict())
        return merged

    # ── Detection ────────────────────────────────────────────────────

    def detect_page(self, device: Any, timeout: float = 0.8) -> str | None:
        """Detect which page the device is currently on.

        Dumps the UI hierarchy once and matches all indicators in memory.
        This is O(1) ADB calls instead of O(pages × indicators).

        Disabled pages are skipped entirely. Unverified pages have their
        effective priority halved so verified pages win on ties.
        """
        try:
            xml = device.dump_hierarchy()
        except Exception:
            logger.warning(f"detect_page({self.app}): dump_hierarchy failed")
            return None

        pages = self.get_pages()

        def _effective_priority(state: AppState) -> int:
            if state.is_disabled:
                return -1
            if not state.is_verified:
                return state.priority // 2
            return state.priority

        ordered = sorted(pages.items(),
                         key=lambda kv: _effective_priority(kv[1]),
                         reverse=True)

        best: tuple[int, int, str] | None = None
        for pid, state in ordered:
            if state.is_disabled:
                continue
            threshold = state.match_threshold
            matched = 0
            for ind in state.indicators:
                text = ind["text"]
                if text in xml:
                    matched += 1
                    if matched >= threshold:
                        break
            if matched >= threshold:
                eff_pri = _effective_priority(state)
                if best is None or (matched, eff_pri) > (best[0], best[1]):
                    best = (matched, eff_pri, pid)
        if best is None:
            return None
        logger.debug(
            f"detect_page({self.app}): {best[2]} (matched {best[0]})")
        return best[2]

    def find_path(self, start: str, goal: str) -> list[str] | None:
        """BFS shortest path from start to goal page."""
        if start == goal:
            return [start]
        pages = self.get_pages()
        if start not in pages or goal not in pages:
            return None
        visited = {start}
        queue: deque[list[str]] = deque([[start]])
        while queue:
            path = queue.popleft()
            current = path[-1]
            state = pages.get(current)
            if not state:
                continue
            for neighbor in state.transitions:
                if neighbor == goal:
                    return path + [neighbor]
                if neighbor not in visited and neighbor in pages:
                    visited.add(neighbor)
                    queue.append(path + [neighbor])
        return None

    def get_transition_hint(self, from_page: str, to_page: str) -> str | None:
        pages = self.get_pages()
        state = pages.get(from_page)
        if not state:
            return None
        trans = state.transitions.get(to_page)
        return trans.get("hint") if trans else None

    def has_transition(self, from_page: str, to_page: str) -> bool:
        """Check if a direct transition edge exists."""
        pages = self.get_pages()
        state = pages.get(from_page)
        return bool(state and to_page in state.transitions)

    # ── Hash-based matching ─────────────────────────────────────────

    _MAX_HASHES_PER_PAGE = 5

    def find_closest_page_by_hash(
        self, query_hash: str,
    ) -> tuple[str | None, int]:
        """Find the page whose screenshot hashes are closest to *query_hash*.

        Returns ``(state_id, hamming_distance)`` or ``(None, 999)``
        if no pages have stored hashes.
        """
        from openclaw_agent.engine.learning.screenshot_hash import hamming_distance

        pages = self.get_pages()
        best_id: str | None = None
        best_dist = 999

        for pid, state in pages.items():
            if state.is_disabled:
                continue
            for stored_hash in state.screenshot_hashes:
                try:
                    dist = hamming_distance(query_hash, stored_hash)
                except ValueError:
                    continue
                if dist < best_dist:
                    best_dist = dist
                    best_id = pid

        return best_id, best_dist

    def add_screenshot_hash(self, state_id: str, new_hash: str) -> None:
        """Append a perceptual hash for a page, deduping and capping at 5."""
        state = self._learned.get(state_id)
        if not state:
            if state_id not in self._seed:
                return
            # Seed-only state: create a minimal learned shell so the hash
            # gets persisted.  The merge logic overlays seed fields at
            # runtime, so we only need state_id + screenshot_hashes here.
            seed = self._seed[state_id]
            state = AppState(
                state_id=state_id,
                name=seed.name,
                source="seed",
            )
            self._learned[state_id] = state
        if new_hash in state.screenshot_hashes:
            return
        state.screenshot_hashes.append(new_hash)
        if len(state.screenshot_hashes) > self._MAX_HASHES_PER_PAGE:
            state.screenshot_hashes = state.screenshot_hashes[
                -self._MAX_HASHES_PER_PAGE:]
        self.save()

    # ── Discovery / Learning ─────────────────────────────────────────

    def record_page_visit(self, state_id: str, name: str = "",
                          indicator_texts: list[str] | None = None) -> None:
        """Record that a page was visited, updating seen_count.

        Seed pages are ground truth and don't need visit tracking.
        Auto-promotes unverified discovered pages to verified once
        seen_count reaches VERIFY_SEEN_THRESHOLD.
        """
        if state_id in self._seed:
            return
        if state_id not in self._learned:
            self._learned[state_id] = AppState(
                state_id=state_id,
                name=name or state_id,
                source="discovered",
                verification="unverified",
            )
        state = self._learned[state_id]
        state.seen_count += 1
        state.last_seen = time.time()
        if name and not state.name:
            state.name = name

        # Auto-promote to verified after enough observations
        if (state.verification == "unverified"
                and state.seen_count >= VERIFY_SEEN_THRESHOLD):
            state.verification = "verified"
            logger.info(
                f"Page '{state_id}' auto-promoted to verified "
                f"(seen {state.seen_count} times)")

        if indicator_texts:
            existing = {i["text"] for i in state.indicators}
            for text in indicator_texts:
                if text not in existing:
                    state.indicators.append({
                        "text": text,
                        "type": "textContains",
                        "source": "discovered",
                    })
                    existing.add(text)
        self.save()

    def record_transition(self, from_page: str, to_page: str,
                          hint: str = "") -> None:
        """Record a transition between two pages."""
        if from_page not in self._learned:
            self._learned[from_page] = AppState(
                state_id=from_page, name=from_page,
                source="discovered",
            )
        state = self._learned[from_page]
        if to_page in state.transitions:
            state.transitions[to_page]["seen_count"] = (
                state.transitions[to_page].get("seen_count", 0) + 1
            )
        else:
            state.transitions[to_page] = {
                "hint": hint,
                "source": "discovered",
                "seen_count": 1,
            }
        self.save()

    def register_state(
        self,
        state_id: str,
        name: str,
        description: str,
        indicators: list[str],
        discovery_path: list[str],
        is_optional: bool = False,
        from_page: str | None = None,
    ) -> None:
        """Register a newly discovered state from vision LLM analysis.

        New states start as ``unverified`` and won't participate in
        cross-node sync until auto-promoted to ``verified`` via
        repeated observations (see ``record_page_visit``).
        """
        self._learned[state_id] = AppState(
            state_id=state_id,
            name=name,
            description=description,
            discovery_path=discovery_path,
            indicators=[
                {"text": t, "type": "textContains", "source": "discovered"}
                for t in indicators
            ],
            is_optional=is_optional,
            priority=10,
            match_threshold=min(2, len(indicators)),
            seen_count=1,
            last_seen=time.time(),
            source="discovered",
            verification="unverified",
        )
        if from_page:
            self.record_transition(from_page, state_id,
                                   hint="VLM 探索发现")
        else:
            self.save()
        logger.info(
            f"Registered new state: {state_id} ({name}) "
            f"for {self.app} [unverified]")

    # ── Failure tracking & auto-pruning ────────────────────────────

    def record_match_failure(self, state_id: str) -> None:
        """Record that a detect_page match for this state led to failure.

        When match_failures / (seen_count + match_failures) exceeds
        PRUNE_FAILURE_RATIO, the page is auto-disabled.
        """
        state = self._learned.get(state_id)
        if not state or state.source == "seed":
            return
        state.match_failures += 1
        total = state.seen_count + state.match_failures
        if total >= 3 and state.match_failures / total >= PRUNE_FAILURE_RATIO:
            state.verification = "disabled"
            logger.warning(
                f"Page '{state_id}' auto-disabled: "
                f"{state.match_failures}/{total} failures")
        self.save()

    def auto_prune_stale(self) -> int:
        """Disable learned pages that haven't been seen in PRUNE_STALE_DAYS.

        Returns the number of pages pruned.
        """
        cutoff = time.time() - PRUNE_STALE_DAYS * 86400
        pruned = 0
        for state in self._learned.values():
            if state.source == "seed":
                continue
            if state.verification == "disabled":
                continue
            if state.last_seen > 0 and state.last_seen < cutoff:
                state.verification = "disabled"
                logger.info(
                    f"Page '{state.state_id}' auto-pruned: "
                    f"last seen {(time.time() - state.last_seen) / 86400:.0f}d ago")
                pruned += 1
        if pruned:
            self.save()
        return pruned

    def get_verified_pages(self) -> dict[str, AppState]:
        """Return only verified pages — used for cross-node sync.

        Seed pages are always verified. Discovered pages need
        seen_count >= VERIFY_SEEN_THRESHOLD.
        """
        return {
            pid: state for pid, state in self.get_pages().items()
            if state.is_verified
        }

    # ── LLM prompt doc ───────────────────────────────────────────────

    def build_page_graph_doc(self) -> str:
        """Build human-readable graph doc for LLM prompt injection."""
        pages = self.get_pages()
        lines = [f"## {self.app} APP 页面状态图\n"]
        lines.append("以下是已知的页面状态和导航关系（持续自动学习更新中）。")
        lines.append("recipe 应在执行前先检测当前页面，然后规划导航路径。\n")
        if self.initial_state:
            lines.append(f"**初始状态**（APP 重启后默认页面）：`{self.initial_state}`\n")
        lines.append("### 页面列表\n")
        for pid, state in sorted(pages.items(), key=lambda x: -x[1].priority):
            source_tag = ""
            if state.source == "discovered":
                source_tag = " [自动发现]"
            opt_tag = " [可选/弹窗]" if state.is_optional else ""
            lines.append(f"- **{pid}**（{state.name}）{source_tag}{opt_tag}")
            if state.description:
                lines.append(f"  - 描述：{state.description}")
            ind_texts = [i["text"] for i in state.indicators]
            lines.append(f"  - 识别标志：{', '.join(ind_texts)}")
            if state.discovery_path:
                lines.append(
                    f"  - 到达路径：{' → '.join(state.discovery_path)}")
            for target, trans in state.transitions.items():
                hint = trans.get("hint", "")
                seen = trans.get("seen_count", 0)
                confidence = f" (观测 {seen} 次)" if seen > 0 else ""
                lines.append(f"  - → {target}：{hint}{confidence}")
            lines.append("")
        lines.append("### 导航策略\n")
        lines.append("1. 先用 `detect_page(device)` 确认当前位置")
        lines.append("2. 如果已在目标页面，直接返回 True")
        lines.append("3. 如果在已知页面，根据状态图转移关系导航")
        lines.append("4. 未知页面先 `back()` 尝试回到已知页面")
        return "\n".join(lines)


# ── Singleton per app ────────────────────────────────────────────────

_graphs: dict[str, AppGraph] = {}
_default_data_dir: str | None = None


def set_app_graph_data_dir(data_dir: str) -> None:
    """Set the default data directory for all AppGraph instances.

    Call this at startup before any get_app_graph() call to redirect
    learned graph storage to a workspace-specific directory.
    """
    global _default_data_dir
    _default_data_dir = data_dir


def get_app_graph(app: str, data_dir: str | None = None) -> AppGraph:
    """Get the singleton AppGraph for an app.

    Seed data is loaded from ``app_pages/<app>.seed.json``.
    Hot-reload: edits to seed or learned JSON take effect without restart.
    """
    if app not in _graphs:
        _graphs[app] = AppGraph(app, data_dir=data_dir or _default_data_dir)
    return _graphs[app]
