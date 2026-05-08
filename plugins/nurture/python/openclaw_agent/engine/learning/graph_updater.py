"""Lightweight per-step page-change tracker for NL operations.

Hooks into PhoneAgent's ``_step_callback`` to detect page transitions
during a NaturalLanguageOperation run and update the app graph in
real time — hash check on every action (~50 ms), heavier identification
only when the page actually changes.

Unknown pages are persisted to a JSONL file for asynchronous VLM
discovery (same pattern as InFlightTraceWriter / RecipeGenerator).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app_graph import get_app_graph
from .screenshot_hash import compute_phash, hamming_distance

logger = logging.getLogger("graph_updater")

_SAME_PAGE_THRESHOLD = 12

_PENDING_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "data" / "pending_discoveries"
)


class PendingDiscoveryWriter:
    """Append-only JSONL writer for unknown-page captures."""

    def __init__(self, app: str, base_dir: Path | None = None):
        self._dir = base_dir or _PENDING_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{app}.jsonl"

    def append(self, record: dict[str, Any]) -> None:
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("pending discovery write failed", exc_info=True)


class PageChangeTracker:
    """Detects page changes after each VLM action and updates the app graph.

    Three-tier identification (cheap -> expensive):
      1. Perceptual hash lookup against known pages
      2. Text indicator detection (``dump_hierarchy``)
      3. Capture screenshot + persist to JSONL for async VLM discovery
    """

    _SKIP_ACTION_TYPES = frozenset({"Type", "Wait", "note", "call_api"})

    def __init__(self, device: Any, app: str) -> None:
        self.device = device
        self.app = app
        self.graph = get_app_graph(app)
        self.last_hash: str | None = None
        self.nav_path: list[str] = []
        self._writer = PendingDiscoveryWriter(app)

    def on_step(self, trace_entry: dict[str, Any]) -> None:
        """Called after each VLM action. Fast-path for same-page actions."""
        action_type = trace_entry.get("action_type", "")
        if action_type in self._SKIP_ACTION_TYPES:
            return

        try:
            current_hash = compute_phash(self.device)
        except Exception:
            logger.debug("phash capture failed, skipping step")
            return

        if self.last_hash is not None:
            distance = hamming_distance(self.last_hash, current_hash)
            if distance < _SAME_PAGE_THRESHOLD:
                self.last_hash = current_hash
                return
            logger.info(
                "[PageTracker] page changed (hash distance=%d), identifying...",
                distance,
            )
        else:
            logger.info("[PageTracker] first step, identifying current page...")

        self.last_hash = current_hash
        self._identify_page(current_hash)

    # ------------------------------------------------------------------

    def _identify_page(self, current_hash: str) -> None:
        match = self.graph.find_closest_page_by_hash(current_hash)
        if match:
            state_id, dist = match
            if dist < _SAME_PAGE_THRESHOLD:
                self._record_known(state_id, current_hash)
                return

        try:
            detected = self.graph.detect_page(self.device, timeout=0.8)
        except Exception:
            detected = None

        if detected:
            self._record_known(detected, current_hash)
            return

        self._capture_and_persist(current_hash)

    def _record_known(self, state_id: str, current_hash: str) -> None:
        logger.info("[PageTracker] identified known page: '%s'", state_id)
        self.graph.record_page_visit(state_id)
        self.graph.add_screenshot_hash(state_id, current_hash)
        if self.nav_path and self.nav_path[-1] != state_id:
            self.graph.record_transition(self.nav_path[-1], state_id)
        if not self.nav_path or self.nav_path[-1] != state_id:
            self.nav_path.append(state_id)

    def _capture_and_persist(self, current_hash: str) -> None:
        """Capture screenshot immediately and write to JSONL for later VLM."""
        try:
            from .vision_utils import take_screenshot_b64
            screenshot_b64 = take_screenshot_b64(self.device)
        except Exception:
            logger.debug("screenshot capture failed for pending discovery")
            return

        record = {
            "id": uuid.uuid4().hex[:12],
            "app": self.app,
            "screenshot_b64": screenshot_b64,
            "phash": current_hash,
            "nav_path": list(self.nav_path),
            "device_id": getattr(self.device, "serial", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "consumed": False,
        }
        self._writer.append(record)
        logger.info(
            "[PageTracker] unknown page (hash=%s), captured and persisted",
            current_hash[:8],
        )


# ---------------------------------------------------------------------------
# Async discovery consumer (called by background loop / HTTP endpoint)
# ---------------------------------------------------------------------------


def process_pending_discoveries(app: str | None = None) -> int:
    """Consume pending discovery JSONL entries and run VLM identification.

    If *app* is given, only process that app's file. Otherwise scan all
    ``*.jsonl`` files in the pending directory.

    Returns the number of new pages discovered.
    """
    base = _PENDING_DIR
    if not base.exists():
        return 0

    files = [base / f"{app}.jsonl"] if app else sorted(base.glob("*.jsonl"))
    discovered = 0

    for path in files:
        if not path.exists():
            continue
        app_name = path.stem
        discovered += _process_file(path, app_name)

    return discovered


def _process_file(path: Path, app: str) -> int:
    """Process one app's pending discovery file. Returns count of new pages."""
    from .state_discovery import discover_state_from_capture

    lines: list[str] = []
    pending: list[dict[str, Any]] = []
    for raw in open(path, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            lines.append(raw)
            continue
        if rec.get("consumed"):
            lines.append(json.dumps(rec, ensure_ascii=False))
            continue
        pending.append(rec)
        lines.append(raw)

    if not pending:
        return 0

    logger.info(
        "[Discovery] processing %d pending entries for app='%s'",
        len(pending), app,
    )

    graph = get_app_graph(app)
    discovered = 0
    changed_ids: set[str] = set()

    for rec in pending:
        rid = rec["id"]
        img_b64 = rec.get("screenshot_b64", "")
        phash = rec.get("phash", "")
        nav_path = rec.get("nav_path", [])

        if not img_b64:
            changed_ids.add(rid)
            continue

        try:
            result = discover_state_from_capture(img_b64, graph, nav_path)
        except Exception:
            logger.debug("discover_state_from_capture failed", exc_info=True)
            continue

        if result is None:
            changed_ids.add(rid)
            continue

        changed_ids.add(rid)

        if result.is_new:
            graph.register_state(
                result.state_id,
                result.name,
                result.description,
                result.indicators,
                list(nav_path),
                result.is_optional,
                from_page=nav_path[-1] if nav_path else None,
            )
            graph.add_screenshot_hash(result.state_id, phash)
            discovered += 1
            logger.info(
                "[Discovery] new page '%s' (%s) for app='%s'",
                result.state_id, result.name, app,
            )
        else:
            graph.record_page_visit(result.state_id)
            graph.add_screenshot_hash(result.state_id, phash)
            if nav_path:
                from_page = nav_path[-1] if nav_path else None
                if from_page and from_page != result.state_id:
                    graph.record_transition(from_page, result.state_id)
            logger.info(
                "[Discovery] matched existing page '%s' for app='%s'",
                result.state_id, app,
            )

    if changed_ids:
        _mark_consumed(path, changed_ids)

    return discovered


def _mark_consumed(path: Path, ids: set[str]) -> None:
    """Rewrite JSONL file marking entries as consumed (strip screenshot)."""
    out: list[str] = []
    for raw in open(path, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            out.append(raw)
            continue
        if rec.get("id") in ids:
            rec["consumed"] = True
            rec.pop("screenshot_b64", None)
        out.append(json.dumps(rec, ensure_ascii=False))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
