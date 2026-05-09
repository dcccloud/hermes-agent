"""Build a prior-attempt prompt fragment for resumable VLM tasks.

When a task is detected as a continuation of a previously timed-out
attempt, the executor pulls the in-flight VLM step trail and turns it
into a short natural-language summary that gets prepended to every
subsequent op's VLM prompt. The intent is for the VLM to inherit
exploration knowledge ("I already tapped settings -> membership ->
saw a paywall") instead of redoing the same UI walk.

The summary is best-effort: when truncation is needed, the first few
steps and the most recent steps are kept (they carry the most signal),
and the middle is collapsed into "... N steps elided ...".
"""

from __future__ import annotations

from typing import Any, Dict, List


def _summarize_step(step: Dict[str, Any]) -> str:
    """Format one step entry as a single human-readable line."""
    action_type = step.get("action_type") or "unknown"
    parts = [f"{action_type}"]
    if "element" in step:
        parts.append(f"element={step['element']!r}")
    if "start" in step:
        parts.append(f"start={step['start']}")
    if "end" in step:
        parts.append(f"end={step['end']}")
    if "text" in step:
        text = str(step["text"])[:60]
        parts.append(f"text={text!r}")
    if step.get("finish_message"):
        msg = str(step["finish_message"])[:80]
        parts.append(f"finish={msg!r}")
    thinking = step.get("thinking")
    if thinking:
        # Keep thinking short — it's the most useful signal but also
        # the most verbose. One sentence max.
        snippet = str(thinking).strip().replace("\n", " ")[:120]
        if snippet:
            parts.append(f"thinking={snippet!r}")
    success = step.get("success")
    if success is False:
        parts.append("(failed)")
    return " ".join(parts)


def build_prior_context_prompt(
    in_flight_records: List[Dict[str, Any]],
    max_actions: int = 30,
) -> str:
    """Render the in-flight step trail as a prompt prefix.

    Args:
        in_flight_records: list of records as returned by
            ``trace_store.read_in_flight_steps(task_id)``. Each record
            is ``{task_id, op_index, op_name, device_id, step, timestamp}``.
        max_actions: keep at most this many step lines. When the trail
            is longer, retain the first 3 and the last (max_actions-3)
            so both the start and the most recent state are visible.

    Returns:
        The summary fragment, or an empty string if no records.
    """
    if not in_flight_records:
        return ""

    # Group steps by op_index, preserving append order.
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    op_names: Dict[int, str] = {}
    for rec in in_flight_records:
        idx = rec.get("op_index", -1)
        grouped.setdefault(idx, []).append(rec.get("step", {}))
        op_names.setdefault(idx, rec.get("op_name", f"op[{idx}]"))

    total_steps = sum(len(v) for v in grouped.values())

    # Truncate global step count if too long.
    keep_head = 3
    keep_tail = max(max_actions - keep_head, 1)
    elide = total_steps > max_actions

    # Build flat ordered step list
    flat: List[tuple[int, str, Dict[str, Any]]] = []
    for idx in sorted(grouped.keys()):
        for s in grouped[idx]:
            flat.append((idx, op_names[idx], s))

    if elide:
        head = flat[:keep_head]
        tail = flat[-keep_tail:]
        skipped = total_steps - len(head) - len(tail)
    else:
        head = flat
        tail = []
        skipped = 0

    lines: List[str] = []
    lines.append(
        "[Continuation context] The previous attempt at this task was "
        "interrupted (likely a timeout). Below is what the visual agent "
        "already explored. Use it to skip redundant UI walks; the device "
        "state may have changed, so verify the current screen before "
        "repeating actions."
    )
    lines.append("")

    last_op: int | None = None
    for idx, op_name, step in head:
        if idx != last_op:
            lines.append(f"[op #{idx}: {op_name}]")
            last_op = idx
        lines.append(f"  step {step.get('step', '?')}: {_summarize_step(step)}")

    if elide:
        lines.append(f"  ... {skipped} earlier step(s) elided ...")
        last_op = None
        for idx, op_name, step in tail:
            if idx != last_op:
                lines.append(f"[op #{idx}: {op_name}]")
                last_op = idx
            lines.append(f"  step {step.get('step', '?')}: {_summarize_step(step)}")

    lines.append("")
    lines.append("[End of continuation context]")
    return "\n".join(lines)
