"""Shared persistence helpers for JSON-backed stores.

JSON backend uses synchronous file IO (matches OpenClaw's
``fs.readFileSync``/``writeFileSync``). Reads happen on construction;
writes happen on every mutation. Tradeoff: simple, no concurrency
issues for single-process community server. Phase 2+ optimization can
batch writes if needed.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def now_ms() -> int:
    """Current Unix epoch in milliseconds (matches JS ``Date.now()``)."""
    return int(time.time() * 1000)


def load_json(file: Path, fallback: Any) -> Any:
    """Load JSON from *file*, or return *fallback* on any read/parse error."""
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def save_json(file: Path, data: Any) -> None:
    """Persist *data* to *file* (creates parent dirs as needed)."""
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
