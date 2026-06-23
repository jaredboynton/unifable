#!/usr/bin/env python3
"""Spec CLI notifications to the main model via PostToolUse additionalContext.

spec.py emits prefixed stderr lines; gate_post_tool.py parses them (or reloads the
spec from disk) and forwards headline, judge commentary, and the task board.
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

_TASK_ID_RE = re.compile(r"\bT\d+\b")
_STOP_VALIDATE_CONTEXT_MAX = 16000
# Statuses whose judge reason is always worth re-showing: still-actionable work.
# `retracted` is resolved/done, so it is NOT here -- a retracted task only shows
# its judge reason on the stop it changed (its id appears in the headlines), and
# otherwise collapses into the done-count line.
_JUDGE_INLINE_STATUSES = frozenset({"failed", "rejected_approach"})
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


def _all_tasks_validated(spec: dict[str, Any]) -> tuple[bool, list[str]]:
    from spec import all_tasks_validated

    return all_tasks_validated(spec)


def format_spec_status(
    spec: dict[str, Any],
    *,
    highlight_task: str | None = None,
    show_judge_for: frozenset[str] | None = None,
    collapse_resolved: bool = False,
) -> str:
    """Compact task board matching the status CLI output shape.

    With ``collapse_resolved=True`` (model-facing contexts), resolved tasks
    (validated/retracted) that are not highlighted or in ``show_judge_for`` fold
    into a single ``done (N): T1, T2`` line instead of a full row each -- a task
    that is already done needs only "done", not a re-narrated row every stop. The
    human ``unifable status`` CLI leaves this False so it stays full.
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
        if collapse_resolved and status in _RESOLVED_STATUSES and not shown:
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
            hint = str(task.get("judge_hint") or "").strip()
            if hint:
                row += f"\n    hint: {hint}"
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
    judge_reason: str | None = None,
    hint: str | None = None,
) -> None:
    """Emit headline, optional full judge commentary, an optional advisory hint,
    and the current task board."""
    notify_model(headline)
    if judge_reason:
        _emit_judge(judge_reason)
    if hint:
        _emit_hint(hint)
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
    ids: set[str] = set()
    for headline in headlines:
        ids.update(_TASK_ID_RE.findall(str(headline or "")))
    return ids


def _stop_validate_judge_tasks(spec: dict[str, Any], headlines: list[str]) -> frozenset[str]:
    _, incomplete = _all_tasks_validated(spec)
    show = set(incomplete) | _task_ids_from_headlines(headlines)
    for task in spec.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        tid = str(task.get("id") or "")
        status = str(task.get("status") or "")
        if not tid or not str(task.get("judge_reason") or "").strip():
            continue
        if status in _JUDGE_INLINE_STATUSES or tid in show:
            show.add(tid)
    return frozenset(show)


def build_stop_validate_context(spec: dict[str, Any], headlines: list[str]) -> str:
    """Format Stop-time auto_validate results for model feedback.

    The headlines above the board are the per-stop delta. Judge/hint text is
    rendered once -- inline in the board (see ``format_spec_status``) -- not also
    as a flat ``Tn judge:`` preamble; and resolved tasks not changed this stop
    collapse to a done-count line.
    """
    msgs = [str(h).strip() for h in (headlines or []) if str(h).strip()]
    if not msgs:
        return ""
    parts: list[str] = ["unifable spec update (stop validation):"]
    parts.extend(msgs)
    show_judge_for = _stop_validate_judge_tasks(spec, msgs)
    parts.append(format_spec_status(spec, show_judge_for=show_judge_for, collapse_resolved=True))
    body = "\n".join(parts)
    if len(body) > _STOP_VALIDATE_CONTEXT_MAX:
        return body[: _STOP_VALIDATE_CONTEXT_MAX - 3] + "..."
    return body


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
    """Merge parsed headline, judge block, and status board for additionalContext."""
    parts: list[str] = []
    headlines = extract_model_notifications(text)
    if headlines:
        parts.extend(headlines)
    judges = extract_all_judge_commentary(text)
    for judge in judges:
        parts.append(f"Judge: {judge}")
    hint = extract_hint(text)
    if hint:
        parts.append(f"Hint: {hint}")
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
