#!/usr/bin/env python3
"""Spec CLI notifications to the main model via PostToolUse additionalContext.

spec.py emits prefixed stderr lines; gate_post_tool.py parses them (or reloads the
spec from disk) and forwards the headline plus one compact next action.
"""

from __future__ import annotations

import hashlib
import re
import sys
from typing import Any

NOTIFY_PREFIX = "UNIFABLE_MODEL_MESSAGE\t"
STATUS_PREFIX = "UNIFABLE_SPEC_STATUS\t"
ACTION_PREFIX = "UNIFABLE_MODEL_ACTION\t"
JUDGE_PREFIX = "UNIFABLE_MODEL_JUDGE\t"
HINT_PREFIX = "UNIFABLE_MODEL_HINT\t"
_HEADLINE_MAX = 320

_SPEC_CLI_RE = re.compile(r"(?i)(?:unifable(?:-spec)?|scripts/gate/spec\.py|/gate/spec\.py)")
_SUBCMD_RE = re.compile(
    r"(?i)(?:unifable(?:-spec)?|scripts/gate/spec\.py|/gate/spec\.py)\s+"
    r"(restate|add-task|set-primary|add-frontier|dispute|contract|where)\b"
)

MUTATING_SUBCMDS = frozenset({"restate", "add-task", "set-primary", "add-frontier", "dispute"})

_TASK_ID_RE = re.compile(r"\bT(\d+)\b")
_STATUS_ROW_RE = re.compile(r"^\s+\[[^\]]+\]\s+(T\d+)\s+\([^)]+\)\s+(.+)$")
_RETRACT_HEADLINE_RE = re.compile(r"^Judge retracted (T\d+):\s*(.+)$", re.IGNORECASE)
_STOP_VALIDATE_CONTEXT_MAX = 16000
_RESOLVED_STATUSES = frozenset({"validated", "retracted", "superseded"})

_STATUS_MARKS = {
    "validated": "OK",
    "failed": "XX",
    "delivered": "..",
    "pending": "--",
    "disputed": "??",
    "retracted": "~~",
    "superseded": "SS",
    "blocked": "BL",
    "rejected_approach": "RJ",
    "accepted_approach": "AC",
}

# Structural gaps from all_tasks_validated / all_tasks_validated_heavy (not task rows).
_SYNTHETIC_INCOMPLETE: dict[str, tuple[str, str]] = {
    "<no requirements added yet>": (
        "requirements (none yet)",
        "Add at least one: `unifable add-task --title '<requirement>' --check '<runnable check>'`.",
    ),
    "<need >=2 frontier approach tasks>": (
        "frontier approaches (need >=2)",
        "HEAVY declare: add >=2 with `unifable add-frontier --title '...' --check '...'` (judge may auto-add during research).",
    ),
    "<need primary approach task>": (
        "primary approach (missing)",
        "HEAVY declare: set the evidence-backed fallback with "
        "`unifable set-primary --title '...' --check '<runnable proof>'` "
        "(stays blocked until all frontiers are rejected).",
    ),
}


def _synthetic_incomplete_label(tid: str) -> str | None:
    entry = _SYNTHETIC_INCOMPLETE.get(tid)
    return entry[0] if entry else None


def _synthetic_incomplete_action(tid: str) -> str | None:
    entry = _SYNTHETIC_INCOMPLETE.get(tid)
    return entry[1] if entry else None


def _all_tasks_validated(spec: dict[str, Any]) -> tuple[bool, list[str]]:
    from spec import all_tasks_validated

    return all_tasks_validated(spec)


def _tasks_by_id(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(t.get("id")): t for t in (spec.get("tasks") or []) if isinstance(t, dict) and t.get("id")}


def _sort_task_ids(ids: set[str]) -> list[str]:
    def key(tid: str) -> tuple[int, str]:
        m = _TASK_ID_RE.search(tid)
        return (int(m.group(1)), tid) if m else (0, tid)

    return sorted(ids, key=key)


def format_spec_status(
    spec: dict[str, Any],
    *,
    highlight_task: str | None = None,
    show_judge_for: frozenset[str] | None = None,
    collapse_resolved: bool = False,
    incomplete_only: bool = False,
) -> str:
    """Compact task board matching the status CLI output shape.

    With ``collapse_resolved=True`` (model-facing contexts), resolved tasks
    (validated/retracted) that are not highlighted or in ``show_judge_for`` fold
    into a single ``done (N): T1, T2`` line instead of a full row each -- a task
    that is already done needs only "done", not a re-narrated row every stop. The
    human ``unifable status`` CLI leaves this False so it stays full.

    With ``incomplete_only=True`` (Stop board section), resolved tasks always
    collapse into the done-count line.
    """
    ok, incomplete = _all_tasks_validated(spec)
    lines = [f"goal: {str(spec.get('restated_goal', ''))[:100]}"]
    try:
        from heavy_workflow import format_approach_board

        if spec.get("heavy_workflow") or any(
            isinstance(t, dict) and str(t.get("approach_kind") or "") in ("frontier", "primary")
            for t in (spec.get("tasks") or [])
        ):
            lines.append(format_approach_board(spec))
    except Exception:
        pass
    highlight = str(highlight_task or "").strip()
    judge_tasks = show_judge_for or frozenset()
    collapsed: list[str] = []
    for task in spec.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        tid = str(task.get("id") or "")
        status = str(task.get("status") or "")
        shown = (highlight and tid == highlight) or tid in judge_tasks
        if (collapse_resolved or incomplete_only) and status in _RESOLVED_STATUSES and not shown:
            collapsed.append(tid)
            continue
        mark = _STATUS_MARKS.get(status, "??")
        kind = str(task.get("approach_kind") or "req")
        title = str(task.get("title") or "")
        row = f"  [{mark}] {tid} ({kind}) {title}"
        if shown:
            reason = str(task.get("judge_reason") or "").strip()
            if reason:
                row += f"\n    judge: {reason}"
        lines.append(row)
    if collapsed:
        lines.append(f"  done ({len(collapsed)}): {', '.join(collapsed)}")
    if ok:
        lines.append("breaker: OPEN (all tasks validated)")
    else:
        lines.append(f"breaker: CLOSED ({len(incomplete)} left: {', '.join(incomplete)})")
    return "\n".join(lines)


def notify_model(message: str) -> None:
    """Print a short headline notification line."""
    msg = " ".join(str(message or "").split())
    if not msg:
        return
    if len(msg) > _HEADLINE_MAX:
        msg = msg[: _HEADLINE_MAX - 3] + "..."
    print(f"{NOTIFY_PREFIX}{msg}", file=sys.stderr)


def _emit_status(status: str) -> None:
    body = (status or "").strip()
    if not body:
        return
    escaped = body.replace("\n", "\\n")
    print(f"{STATUS_PREFIX}{escaped}", file=sys.stderr)


def _emit_action(action: str) -> None:
    body = (action or "").strip()
    if not body:
        return
    escaped = body.replace("\n", "\\n")
    print(f"{ACTION_PREFIX}{escaped}", file=sys.stderr)


def _emit_judge(reason: str) -> None:
    text = (reason or "").strip()
    if not text:
        return
    print(f"{JUDGE_PREFIX}{text}", file=sys.stderr)


def _emit_hint(hint: str) -> None:
    text = (hint or "").strip()
    if not text:
        return
    print(f"{HINT_PREFIX}{text}", file=sys.stderr)


def notify_spec_update(
    spec: dict[str, Any],
    headline: str,
    *,
    highlight_task: str | None = None,
) -> None:
    """Emit headline, full internal status, and compact model-facing action."""
    notify_model(headline)
    _emit_status(format_spec_status(spec, highlight_task=highlight_task, collapse_resolved=True))
    _emit_action(format_spec_action_digest(spec, highlight_task=highlight_task))


def extract_model_notifications(text: str) -> list[str]:
    """Return headline messages embedded in combined Bash stdout/stderr."""
    out: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith(NOTIFY_PREFIX):
            msg = line[len(NOTIFY_PREFIX) :].strip()
            if msg and msg not in out:
                out.append(msg)
    return out


def extract_action_digests(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith(ACTION_PREFIX):
            body = line[len(ACTION_PREFIX) :].strip()
            if body:
                action = body.replace("\\n", "\n")
                if action not in out:
                    out.append(action)
    return out


def extract_judge_commentary(text: str) -> str | None:
    judges = extract_all_judge_commentary(text)
    return judges[0] if judges else None


def extract_all_judge_commentary(text: str) -> list[str]:
    out: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith(JUDGE_PREFIX):
            body = line[len(JUDGE_PREFIX) :].strip()
            if body and body not in out:
                out.append(body)
    return out


def _task_ids_from_headlines(headlines: list[str]) -> set[str]:
    """Return task ids referenced in headline text."""
    ids: set[str] = set()
    for headline in headlines:
        for m in _TASK_ID_RE.finditer(str(headline or "")):
            ids.add(f"T{m.group(1)}")
    return ids


task_ids_from_headlines = _task_ids_from_headlines


def _consecutive_task_ids(tids: list[str]) -> bool:
    nums = []
    for tid in tids:
        m = _TASK_ID_RE.search(tid)
        if not m:
            return False
        nums.append(int(m.group(1)))
    nums.sort()
    return all(nums[i] + 1 == nums[i + 1] for i in range(len(nums) - 1))


def collapse_stop_headlines(headlines: list[str]) -> list[str]:
    """Collapse repeated loop-release retraction headlines into one line."""
    retract_by_reason: dict[str, list[str]] = {}
    other: list[str] = []
    seen_other: set[str] = set()
    for raw in headlines or []:
        h = str(raw or "").strip()
        if not h:
            continue
        m = _RETRACT_HEADLINE_RE.match(h)
        if m:
            tid, reason = m.group(1).upper(), m.group(2).strip()
            if not tid.startswith("T"):
                tid = f"T{tid.lstrip('Tt')}"
            retract_by_reason.setdefault(reason, []).append(tid)
            continue
        if h not in seen_other:
            seen_other.add(h)
            other.append(h)
    out = list(other)
    for reason, tids in retract_by_reason.items():
        sorted_tids = _sort_task_ids(set(tids))
        if len(sorted_tids) == 1:
            out.append(f"Judge retracted {sorted_tids[0]}: {reason}")
        elif _consecutive_task_ids(sorted_tids):
            out.append(f"Judge retracted {sorted_tids[0]}-{sorted_tids[-1]} (loop release): {reason}")
        else:
            out.append(f"Judge retracted {', '.join(sorted_tids)} (loop release): {reason}")
    return out


def format_stop_action_digest(spec: dict[str, Any], changed_ids: set[str]) -> str:
    """Full judge reasoning + hints for tasks adjudicated this stop."""
    if not changed_ids:
        return ""
    by_id = _tasks_by_id(spec)
    lines: list[str] = []
    for tid in _sort_task_ids(changed_ids):
        task = by_id.get(tid)
        if not task:
            continue
        status = str(task.get("status") or "")
        mark = _STATUS_MARKS.get(status, "??")
        title = str(task.get("title") or "")
        lines.append(f"  {tid} [{mark}] {title}")
        reason = str(task.get("judge_reason") or "").strip()
        if reason:
            lines.append(f"    judge: {reason}")
        hint = str(task.get("judge_hint") or "").strip()
        if hint:
            lines.append(f"    hint: {hint}")
    return "\n".join(lines)


def format_stop_unresolved_actions(spec: dict[str, Any], changed_ids: set[str]) -> str:
    """Stop-facing action list: unresolved tasks only, with fresh guidance inline."""
    ok, incomplete = _all_tasks_validated(spec)
    if ok:
        return "breaker: OPEN (all tasks validated)"
    by_id = _tasks_by_id(spec)
    try:
        from heavy_workflow import task_is_resolved
    except ImportError:
        from scripts.gate.heavy_workflow import task_is_resolved

    seen: set[str] = set()
    ordered = []
    for tid in incomplete:
        stid = str(tid)
        if not stid or stid in seen:
            continue
        task = by_id.get(stid)
        if task is not None and task_is_resolved(task):
            continue
        seen.add(stid)
        ordered.append(stid)
    if not ordered:
        return "breaker: OPEN (all tasks validated)"

    lines = ["Action required:"]
    for tid in ordered:
        task = by_id.get(tid)
        if not task:
            action = _synthetic_incomplete_action(tid)
            if action:
                lines.append(f"  {action}")
            else:
                lines.append(f"  {tid}")
            continue
        status = str(task.get("status") or "")
        mark = _STATUS_MARKS.get(status, "??")
        title = str(task.get("title") or "")
        lines.append(f"  {tid} [{mark}] {title}")
        reason = str(task.get("judge_reason") or "").strip()
        hint = str(task.get("judge_hint") or "").strip()
        if tid in changed_ids and reason:
            lines.append(f"    judge: {reason}")
        if hint:
            lines.append(f"    hint: {hint}")
    display = [_synthetic_incomplete_label(tid) or tid for tid in ordered]
    lines.append(f"breaker: CLOSED ({len(ordered)} left: {', '.join(display)})")
    return "\n".join(lines)


def _hash_field(value: Any) -> str:
    text = str(value or "")
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def task_guidance_fingerprint(spec: dict[str, Any], tid: str) -> dict[str, str]:
    """Stable fingerprint of fields that appear in PostToolUse action digests."""
    task = _tasks_by_id(spec).get(str(tid))
    if not task:
        return {}
    return {
        "status": str(task.get("status") or ""),
        "check": _hash_field(task.get("check")),
        "reason": _hash_field(task.get("judge_reason")),
        "hint": _hash_field(task.get("judge_hint")),
        "title": _hash_field(task.get("title")),
    }


def _action_line_for_task(spec: dict[str, Any], tid: str) -> str:
    """One compact action line for *tid* (same shape as format_spec_action_digest)."""
    by_id = _tasks_by_id(spec)
    task = by_id.get(tid)
    if not task:
        action = _synthetic_incomplete_action(tid)
        if action:
            return action
        label = _synthetic_incomplete_label(tid) or tid
        return f"Next: {label}."
    hint = str(task.get("judge_hint") or "").strip()
    reason = str(task.get("judge_reason") or "").strip()
    title = str(task.get("title") or "").strip()
    detail = hint or reason or title
    if detail:
        return f"{tid}: {detail}"
    return f"{tid}: needs evidence."


def format_spec_action_digest_delta(
    spec: dict[str, Any],
    ledger: dict[str, Any],
    *,
    highlight_task: str | None = None,
    max_items: int = 1,
    force: bool = False,
) -> tuple[str, dict[str, dict[str, str]]]:
    """Return action digest lines only for tasks whose guidance changed since last emit.

    ``force=True`` skips delta filtering (fresh spec-CLI stderr path).
    """
    ok, incomplete = _all_tasks_validated(spec)
    cached_raw = ledger.get("posttool_task_guidance")
    cached: dict[str, dict[str, str]] = cached_raw if isinstance(cached_raw, dict) else {}
    if ok:
        line = "breaker: OPEN (all tasks validated)"
        return line, dict(cached)

    highlight = str(highlight_task or "").strip()
    ordered: list[str] = []
    if highlight and highlight in incomplete:
        ordered.append(highlight)
    for raw in incomplete:
        tid = str(raw)
        if tid and tid not in ordered:
            ordered.append(tid)

    lines: list[str] = []
    new_cache = dict(cached)
    for tid in ordered:
        fp = task_guidance_fingerprint(spec, tid)
        prev = cached.get(tid) if isinstance(cached.get(tid), dict) else {}
        changed = force or highlight == tid or fp != prev
        if not changed:
            continue
        lines.append(_action_line_for_task(spec, tid))
        new_cache[tid] = fp
        if len(lines) >= max_items:
            break
    return "\n".join(lines), new_cache


def guidance_covers_incomplete(spec: dict[str, Any], ledger: dict[str, Any]) -> bool:
    """True when every incomplete task has a cached guidance fingerprint."""
    _ok, incomplete = _all_tasks_validated(spec)
    if not incomplete:
        return True
    cached_raw = ledger.get("posttool_task_guidance")
    cached: dict[str, dict[str, str]] = cached_raw if isinstance(cached_raw, dict) else {}
    for tid in incomplete:
        stid = str(tid)
        if stid.startswith("<"):
            continue
        if stid not in cached:
            return False
        if task_guidance_fingerprint(spec, stid) != cached.get(stid):
            return False
    return True


def build_citation_sync_context(headline: str) -> str:
    """Cite-sync only — never bundle task action digest."""
    return " ".join(str(headline or "").split())


def build_spec_action_context(
    spec: dict[str, Any],
    *,
    highlight_task: str | None = None,
    max_items: int = 1,
) -> str:
    return format_spec_action_digest(
        spec,
        highlight_task=highlight_task,
        max_items=max_items,
    )


def format_spec_action_digest(
    spec: dict[str, Any],
    *,
    highlight_task: str | None = None,
    max_items: int = 1,
) -> str:
    """Compact model-facing next action for PostToolUse spec CLI updates.

    PostToolUse fires immediately after commands the model just ran, so repeating
    the whole task board adds noise. Keep only a breaker-open signal, or the
    next unresolved action the model needs in order to move the breaker.
    """
    ok, incomplete = _all_tasks_validated(spec)
    if ok:
        return "breaker: OPEN (all tasks validated)"

    by_id = _tasks_by_id(spec)
    ordered: list[str] = []
    highlight = str(highlight_task or "").strip()
    if highlight and highlight in incomplete:
        ordered.append(highlight)
    for raw in incomplete:
        tid = str(raw)
        if tid and tid not in ordered:
            ordered.append(tid)

    lines: list[str] = []
    for tid in ordered:
        task = by_id.get(tid)
        if not task:
            action = _synthetic_incomplete_action(tid)
            if action:
                lines.append(action)
            else:
                label = _synthetic_incomplete_label(tid) or tid
                lines.append(f"Next: {label}.")
            continue
        hint = str(task.get("judge_hint") or "").strip()
        reason = str(task.get("judge_reason") or "").strip()
        title = str(task.get("title") or "").strip()
        detail = hint or reason or title
        if detail:
            lines.append(f"{tid}: {detail}")
        else:
            lines.append(f"{tid}: needs evidence.")
        if len(lines) >= max_items:
            break
    return "\n".join(lines)


def _stop_non_task_notes(headlines: list[str]) -> list[str]:
    """Keep Stop guidance that is not tied to a specific task row."""
    notes: list[str] = []
    seen: set[str] = set()
    for headline in collapse_stop_headlines(headlines):
        if _task_ids_from_headlines([headline]):
            continue
        if headline not in seen:
            seen.add(headline)
            notes.append(headline)
    return notes


def _adopted_primary_structural_hint(spec: dict[str, Any], task: dict[str, Any]) -> str | None:
    """Hint when primary blocks in adopted phase but judge_reason is stale/misleading."""
    try:
        from heavy_workflow import adopted_frontier, approach_kind
    except ImportError:
        from scripts.gate.heavy_workflow import adopted_frontier, approach_kind
    if approach_kind(task) != "primary":
        return None
    winner = adopted_frontier(spec)
    if winner is None:
        return None
    status = str(task.get("status") or "")
    if status == "superseded":
        return None
    wid = str(winner.get("id") or "")
    tid = str(task.get("id") or "")
    return (
        f"{tid}: primary must be superseded now that frontier {wid} was adopted (harness auto-heals; do not re-run this check)."
    )


def format_blocking_task_hints(
    spec: dict[str, Any],
    incomplete: list[str],
    *,
    changed_ids: set[str] | None = None,
    max_tasks: int = 5,
) -> str:
    """Short actionable lines for the Stop ``reason`` field."""
    if not incomplete:
        return ""
    changed = changed_ids or set()
    by_id = _tasks_by_id(spec)
    if changed:
        incomplete_set = set(incomplete)
        ordered = [t for t in _sort_task_ids(changed) if t in incomplete_set][:max_tasks]
    else:
        ordered = list(incomplete)[:max_tasks]
    hint_lines: list[str] = []
    for tid in ordered:
        task = by_id.get(tid)
        if not task:
            action = _synthetic_incomplete_action(tid)
            if action:
                hint_lines.append(f"  {action}")
            continue
        structural = _adopted_primary_structural_hint(spec, task)
        if structural:
            hint_lines.append(f"  {structural}")
            continue
        hint = str(task.get("judge_hint") or "").strip()
        reason = str(task.get("judge_reason") or "").strip()
        text = hint or reason
        if text:
            hint_lines.append(f"  {tid}: {text}")
    if not hint_lines:
        return ""
    return "\nAction:\n" + "\n".join(hint_lines)


def _stop_validate_judge_tasks(spec: dict[str, Any], headlines: list[str]) -> frozenset[str]:
    """Tasks that get full judge inline on the Stop board (changed this stop only)."""
    return frozenset(_task_ids_from_headlines(headlines))


def build_stop_validate_context(
    spec: dict[str, Any],
    headlines: list[str],
    *,
    max_len: int | None = None,
) -> tuple[str, bool]:
    """Format Stop-time auto_validate results for model feedback.

    Returns ``(context, truncated)``. The unresolved action block (requirements,
    judge reasoning, hints) is never truncated — only optional Notes may be
    dropped when over ``max_len``. If the action block alone exceeds the budget,
    it is still returned in full and ``truncated`` is True (see persisted digest).
    """
    raw_msgs = [str(h).strip() for h in (headlines or []) if str(h).strip()]
    if not raw_msgs:
        return "", False
    limit = max_len if max_len is not None else _STOP_VALIDATE_CONTEXT_MAX
    changed_ids = _task_ids_from_headlines(raw_msgs)
    action = format_stop_unresolved_actions(spec, changed_ids)
    notes = _stop_non_task_notes(raw_msgs)

    header = "unifable spec update (stop validation):"
    core = f"{header}\n{action}"
    if not notes:
        return core, len(core) > limit

    notes_block = "Notes:\n" + "\n".join(f"  {note}" for note in notes)
    full = f"{core}\n{notes_block}"
    if len(full) <= limit:
        return full, False
    # Drop optional notes only — never shorten judge/hint/requirement guidance.
    return core, True


def extract_hint(text: str) -> str | None:
    for line in (text or "").splitlines():
        if line.startswith(HINT_PREFIX):
            body = line[len(HINT_PREFIX) :].strip()
            if body:
                return body
    return None


def extract_spec_status(text: str) -> str | None:
    for line in (text or "").splitlines():
        if line.startswith(STATUS_PREFIX):
            body = line[len(STATUS_PREFIX) :].strip()
            if body:
                return body.replace("\\n", "\n")
    return None


def _fallback_action_from_status(status: str) -> str:
    text = (status or "").strip()
    if not text:
        return ""
    breaker_line = ""
    for line in text.splitlines():
        if line.startswith("breaker:"):
            breaker_line = line.strip()
            break
    if "breaker: OPEN" in breaker_line:
        return breaker_line
    if "breaker: CLOSED" in breaker_line:
        actions: list[str] = []
        for key in _SYNTHETIC_INCOMPLETE:
            if key in breaker_line:
                action = _synthetic_incomplete_action(key)
                if action:
                    actions.append(action)
        if actions:
            return "\n".join(actions)
        incomplete_ids = {f"T{m.group(1)}" for m in _TASK_ID_RE.finditer(breaker_line)}
        if incomplete_ids:
            rows: dict[str, str] = {}
            judges: dict[str, str] = {}
            current = ""
            for line in text.splitlines():
                row = _STATUS_ROW_RE.match(line)
                if row:
                    current = row.group(1)
                    rows[current] = row.group(2).strip()
                    continue
                stripped = line.strip()
                if current and stripped.startswith("judge:"):
                    judges[current] = stripped.removeprefix("judge:").strip()
            for tid in _sort_task_ids(incomplete_ids):
                detail = judges.get(tid) or rows.get(tid)
                if detail:
                    return f"{tid}: {detail}"
    return ""


def bash_output_text(value: Any, limit: int = 16000) -> str:
    """Stdout+stderr from a Bash tool result, preserving line breaks for parsers."""
    from ledger import SECRET_PATTERNS

    if isinstance(value, dict):
        chunks: list[str] = []
        for key in ("stdout", "stderr", "output", "message", "text", "content"):
            part = value.get(key)
            if isinstance(part, str) and part.strip():
                chunks.append(part.rstrip("\n"))
        text = "\n".join(chunks) if chunks else ""
    else:
        text = str(value or "")
    text = text.replace("\r", "")
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def build_spec_context_from_output(text: str) -> str:
    """Merge parsed headlines and compact actions for additionalContext."""
    parts: list[str] = []
    headlines = extract_model_notifications(text)
    if headlines:
        parts.extend(headlines)
    actions = extract_action_digests(text)
    if actions:
        parts.extend(actions)
    else:
        status = extract_spec_status(text)
        fallback = _fallback_action_from_status(status or "")
        if fallback:
            parts.append(fallback)
    if not parts:
        return ""
    if len(parts) == 2 and "\n" not in parts[0] and "\n" not in parts[1]:
        return f"{parts[0]} {parts[1]}"
    return "\n".join(parts)


def build_spec_context_from_spec(
    spec: dict[str, Any],
    *,
    headlines: list[str] | None = None,
    highlight_task: str | None = None,
    include_action: bool = True,
) -> str:
    parts = [str(h).strip() for h in (headlines or []) if str(h).strip()]
    if include_action:
        action = format_spec_action_digest(spec, highlight_task=highlight_task)
        if action:
            parts.append(action)
    if not parts:
        return ""
    if len(parts) == 2 and "\n" not in parts[0] and "\n" not in parts[1]:
        return f"{parts[0]} {parts[1]}"
    return "\n".join(parts)


def is_spec_cli_command(command: str) -> bool:
    return bool(_SPEC_CLI_RE.search(str(command or "")))


def parse_spec_cli_invocation(command: str) -> tuple[str | None, str | None]:
    """Return (subcommand, task_id). Session id is env-resolved; task_id is always None."""
    cmd = str(command or "")
    sub_match = _SUBCMD_RE.search(cmd)
    subcommand = sub_match.group(1).lower() if sub_match else None
    return subcommand, None


def is_mutating_spec_cli(command: str) -> bool:
    subcommand, _ = parse_spec_cli_invocation(command)
    return subcommand in MUTATING_SUBCMDS
