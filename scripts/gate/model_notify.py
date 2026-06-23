#!/usr/bin/env python3
"""Spec CLI notifications to the main model via PostToolUse additionalContext.

spec.py emits prefixed stderr lines; gate_post_tool.py parses them (or reloads the
spec from disk) and forwards headline plus the task board (judge detail inline).
"""

from __future__ import annotations

import re
import sys
from typing import Any

NOTIFY_PREFIX = "UNIFABLE_MODEL_MESSAGE\t"
STATUS_PREFIX = "UNIFABLE_SPEC_STATUS\t"
JUDGE_PREFIX = "UNIFABLE_MODEL_JUDGE\t"
HINT_PREFIX = "UNIFABLE_MODEL_HINT\t"
_HEADLINE_MAX = 320

_SPEC_CLI_RE = re.compile(r"(?i)(?:unifable(?:-spec)?|scripts/gate/spec\.py|/gate/spec\.py)")
_SUBCMD_RE = re.compile(
    r"(?i)(?:unifable(?:-spec)?|scripts/gate/spec\.py|/gate/spec\.py)\s+"
    r"(restate|add-task|set-primary|add-frontier|dispute|validate|contract|where)\b"
)

MUTATING_SUBCMDS = frozenset({"restate", "add-task", "set-primary", "add-frontier", "dispute"})

_TASK_ID_RE = re.compile(r"\bT(\d+)\b")
_RETRACT_HEADLINE_RE = re.compile(r"^Judge retracted (T\d+):\s*(.+)$", re.IGNORECASE)
_STOP_VALIDATE_CONTEXT_MAX = 16000
_STOP_ACTION_DIGEST_RESERVE = 4000
_BLOCKING_HINT_REASON_MAX = 200
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
}

# Structural gaps from all_tasks_validated / all_tasks_validated_heavy (not task rows).
_SYNTHETIC_INCOMPLETE: dict[str, tuple[str, str]] = {
    "<no requirements added yet>": (
        "requirements (none yet)",
        "Add at least one: `unifable add-task --title '<requirement>' --check '<runnable check>'`.",
    ),
    "<need >=2 frontier approach tasks>": (
        "frontier approaches (need >=2)",
        "HEAVY declare: add >=2 with `unifable add-frontier --title '...' --check '...'` "
        "(judge may auto-add during research).",
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
    return {
        str(t.get("id")): t
        for t in (spec.get("tasks") or [])
        if isinstance(t, dict) and t.get("id")
    }


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
    """Emit headline and the current task board (judge detail inline on highlighted rows)."""
    notify_model(headline)
    _emit_status(format_spec_status(spec, highlight_task=highlight_task, collapse_resolved=True))


def extract_model_notifications(text: str) -> list[str]:
    """Return headline messages embedded in combined Bash stdout/stderr."""
    out: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith(NOTIFY_PREFIX):
            msg = line[len(NOTIFY_PREFIX) :].strip()
            if msg and msg not in out:
                out.append(msg)
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
            out.append(
                f"Judge retracted {sorted_tids[0]}-{sorted_tids[-1]} (loop release): {reason}"
            )
        else:
            out.append(
                f"Judge retracted {', '.join(sorted_tids)} (loop release): {reason}"
            )
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
    return "\n".join(lines)


def format_stop_unresolved_actions(spec: dict[str, Any], changed_ids: set[str]) -> str:
    """Stop-facing action list: unresolved tasks only, with fresh guidance inline."""
    ok, incomplete = _all_tasks_validated(spec)
    if ok:
        return "breaker: OPEN (all tasks validated)"
    by_id = _tasks_by_id(spec)
    seen: set[str] = set()
    ordered = []
    for tid in incomplete:
        stid = str(tid)
        if stid and stid not in seen:
            seen.add(stid)
            ordered.append(stid)

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
        elif hint:
            lines.append(f"    hint: {hint}")
    display = [
        _synthetic_incomplete_label(tid) or tid for tid in ordered
    ]
    lines.append(f"breaker: CLOSED ({len(ordered)} left: {', '.join(display)})")
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
        hint = str(task.get("judge_hint") or "").strip()
        reason = str(task.get("judge_reason") or "").strip()
        text = hint
        if not text and reason:
            text = reason[:_BLOCKING_HINT_REASON_MAX]
            if len(reason) > _BLOCKING_HINT_REASON_MAX:
                text += "..."
        if text:
            hint_lines.append(f"  {tid}: {text}")
    if not hint_lines:
        return ""
    return "\nAction:\n" + "\n".join(hint_lines)


def _stop_validate_judge_tasks(spec: dict[str, Any], headlines: list[str]) -> frozenset[str]:
    """Tasks that get full judge inline on the Stop board (changed this stop only)."""
    return frozenset(_task_ids_from_headlines(headlines))


def _truncate_board_section(board: str, max_len: int) -> str:
    if len(board) <= max_len:
        return board
    trimmed = board[: max(0, max_len - 80)].rstrip()
    return trimmed + "\n(board truncated; run unifable status for full board)"


def build_stop_validate_context(
    spec: dict[str, Any],
    headlines: list[str],
    *,
    max_len: int | None = None,
) -> tuple[str, bool]:
    """Format Stop-time auto_validate results for model feedback.

    Returns ``(context, truncated)``. The Stop context lists unresolved tasks
    only, includes fresh judge detail for tasks changed this stop, and preserves
    non-task loop guidance as notes.
    """
    raw_msgs = [str(h).strip() for h in (headlines or []) if str(h).strip()]
    if not raw_msgs:
        return "", False
    limit = max_len if max_len is not None else _STOP_VALIDATE_CONTEXT_MAX
    changed_ids = _task_ids_from_headlines(raw_msgs)
    action = format_stop_unresolved_actions(spec, changed_ids)
    notes = _stop_non_task_notes(raw_msgs)

    parts: list[str] = ["unifable spec update (stop validation):", action]
    if notes:
        parts.extend(["Notes:", "\n".join(f"  {note}" for note in notes)])
    body = "\n".join(parts)
    if len(body) <= limit:
        return body, False

    body = body[: limit - 3] + "..."
    return body, True


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
    """Merge parsed headlines and status board for additionalContext."""
    parts: list[str] = []
    headlines = extract_model_notifications(text)
    if headlines:
        parts.extend(headlines)
    status = extract_spec_status(text)
    if status:
        parts.append(status)
    if not parts:
        return ""
    return "unifable spec update:\n" + "\n".join(parts)


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
