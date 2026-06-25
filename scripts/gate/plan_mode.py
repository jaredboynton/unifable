"""Detect host Plan Mode from raw transcripts and hook payloads.

Plan mode forbids repo-tracked writes; judges and hooks need an explicit signal
because stripped transcript tails drop host mode markers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_PLAN_MODE_EMPTY: dict[str, Any] = {"enabled": False, "host": "", "marker": ""}

_CURSOR_PLAN_ACTIVE_RE = re.compile(
    r"<system_reminder>[\s\S]*?Plan mode is active",
    re.IGNORECASE,
)
_CURSOR_PLAN_EXITED_RE = re.compile(
    r"You have EXITED your previous mode",
    re.IGNORECASE,
)


def empty_plan_mode() -> dict[str, Any]:
    return dict(_PLAN_MODE_EMPTY)


def _state(enabled: bool, host: str, marker: str) -> dict[str, Any]:
    return {"enabled": bool(enabled), "host": str(host or ""), "marker": str(marker or "")}


def detect_plan_mode_from_prompt(prompt: str) -> dict[str, Any]:
    """Cursor and similar hosts inject plan-mode reminders into the prompt text."""
    text = str(prompt or "")
    if not text.strip():
        return empty_plan_mode()
    if _CURSOR_PLAN_EXITED_RE.search(text):
        return _state(False, "cursor", "prompt:exited_previous_mode")
    if _CURSOR_PLAN_ACTIVE_RE.search(text):
        return _state(True, "cursor", "prompt:plan_mode_active")
    return empty_plan_mode()


def _scan_claude_record(rec: dict[str, Any], state: dict[str, Any]) -> None:
    if rec.get("type") != "attachment":
        return
    att = rec.get("attachment")
    if not isinstance(att, dict) or att.get("isSubAgent"):
        return
    kind = str(att.get("type") or "")
    if kind in ("plan_mode", "plan_mode_reentry"):
        state.update(_state(True, "claude", f"attachment:{kind}"))
    elif kind == "plan_mode_exit":
        state.update(_state(False, "claude", "attachment:plan_mode_exit"))

    msg = rec.get("message")
    if isinstance(msg, dict):
        _scan_message_tool_uses(msg.get("content"), state, host="claude")


def _scan_codex_record(rec: dict[str, Any], state: dict[str, Any]) -> None:
    rtype = rec.get("type")
    if rtype == "turn_context":
        payload = rec.get("payload")
        if isinstance(payload, dict):
            cm = payload.get("collaboration_mode")
            if isinstance(cm, dict):
                mode = str(cm.get("mode") or "")
                if mode:
                    state.update(_state(mode == "plan", "codex", f"turn_context:{mode}"))
        return
    if rtype != "event_msg":
        return
    payload = rec.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "task_started":
        return
    kind = str(payload.get("collaboration_mode_kind") or "")
    if kind:
        state.update(_state(kind == "plan", "codex", f"task_started:{kind}"))


def _tool_use_parts(content: Any) -> list[dict[str, Any]]:
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_use" or part.get("name"):
            out.append(part)
    return out


def _scan_message_tool_uses(content: Any, state: dict[str, Any], *, host: str) -> None:
    for part in _tool_use_parts(content):
        name = str(part.get("name") or "")
        if host == "claude" and name == "ExitPlanMode":
            state.update(_state(False, "claude", "tool:ExitPlanMode"))
            continue
        if name != "SwitchMode":
            continue
        raw = part.get("input")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = {}
        if not isinstance(raw, dict):
            continue
        target = str(raw.get("target_mode_id") or "").strip().lower()
        if target == "plan":
            state.update(_state(True, host, f"tool:SwitchMode:{target}"))
        elif target:
            state.update(_state(False, host, f"tool:SwitchMode:{target}"))


def _scan_cursor_record(rec: dict[str, Any], state: dict[str, Any]) -> None:
    role = str(rec.get("role") or "")
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if role == "assistant":
        _scan_message_tool_uses(content, state, host="cursor")
        return
    if role != "user":
        return
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                texts.append(part)
    combined = "\n".join(texts)
    if _CURSOR_PLAN_EXITED_RE.search(combined):
        state.update(_state(False, "cursor", "user:exited_previous_mode"))
    elif _CURSOR_PLAN_ACTIVE_RE.search(combined):
        state.update(_state(True, "cursor", "user:plan_mode_active"))


def _scan_record(rec: dict[str, Any], state: dict[str, Any]) -> None:
    if "role" in rec and "message" in rec:
        _scan_cursor_record(rec, state)
    if rec.get("type") == "attachment":
        _scan_claude_record(rec, state)
    elif rec.get("type") in ("turn_context", "event_msg"):
        _scan_codex_record(rec, state)


def detect_plan_mode(transcript_path: str | None) -> dict[str, Any]:
    """Scan raw JSONL; last marker wins. Fail open on errors."""
    if not transcript_path:
        return empty_plan_mode()
    path = Path(transcript_path)
    if not path.is_file():
        return empty_plan_mode()
    state = empty_plan_mode()
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                _scan_record(rec, state)
    except OSError:
        return empty_plan_mode()
    return state


def resolve_plan_mode(
    input_data: dict | None,
    *,
    transcript_path: str | None = None,
) -> dict[str, Any]:
    """Merge prompt hint and transcript scan; transcript markers win when present."""
    data = input_data if isinstance(input_data, dict) else {}
    prompt = str(data.get("prompt") or data.get("user_prompt") or "")
    tp = transcript_path or data.get("transcript_path")
    from_prompt = detect_plan_mode_from_prompt(prompt)
    from_transcript = detect_plan_mode(str(tp) if tp else None)
    if from_transcript.get("marker"):
        return from_transcript
    if from_prompt.get("marker"):
        return from_prompt
    return empty_plan_mode()


def resolve_plan_mode_for_hooks(input_data: dict | None) -> dict[str, Any]:
    """PreToolUse: transcript when available, else ledger cache from UserPromptSubmit."""
    data = input_data if isinstance(input_data, dict) else {}
    pm = resolve_plan_mode(data, transcript_path=data.get("transcript_path"))
    if pm.get("marker"):
        return pm
    try:
        from ledger import load_ledger
    except ImportError:
        from scripts.gate.ledger import load_ledger  # pragma: no cover
    ledger = load_ledger(data)
    if ledger.get("plan_mode_enabled"):
        return _state(
            True,
            str(ledger.get("plan_mode_host") or ""),
            "ledger:plan_mode_enabled",
        )
    return pm


def plan_mode_context_line(plan: dict[str, Any]) -> str:
    if not plan.get("enabled"):
        return ""
    host = str(plan.get("host") or "host")
    return (
        f"\n\nHost Plan Mode is active ({host}). "
        "Repo-tracked writes are forbidden this turn. "
        "Deliverable is a plan artifact only "
        "(Cursor: CreatePlan / ~/.cursor/plans; Claude: ExitPlanMode / ~/.claude/plans; "
        "Codex: <proposed_plan> block). "
        "Do not add spec tasks whose checks require new repo files "
        "(test -f, git diff on paths). "
        "Use plan-based checks or unifable dispute with plan-mode evidence on stop."
    )


def plan_mode_spec_task_guidance(plan: dict[str, Any]) -> str:
    if not plan.get("enabled"):
        return ""
    return " Plan Mode: prefer checks that inspect the plan deliverable, not repo paths."


def append_plan_mode_note(message: str, plan: dict[str, Any] | None) -> str:
    if not isinstance(plan, dict) or not plan.get("enabled"):
        return message
    note = (
        "\nPlan Mode active: repo edits blocked by host. "
        "Finish the plan deliverable; use unifable dispute for repo-file "
        "requirements that cannot run until Agent mode."
    )
    msg = str(message or "").rstrip()
    if note.strip() in msg:
        return msg
    return msg + note


def _plan_mode_epoch(input_data: dict[str, Any], ledger: dict[str, Any] | None = None) -> str:
    try:
        from pretool_block import block_epoch
    except ImportError:
        from scripts.gate.pretool_block import block_epoch  # pragma: no cover
    return block_epoch(input_data, ledger)


def plan_mode_prompt_line_needed(
    input_data: dict[str, Any],
    plan: dict[str, Any] | None,
) -> bool:
    """Emit full plan-mode context once per turn epoch."""
    if not isinstance(plan, dict) or not plan.get("enabled"):
        return False
    try:
        from ledger import load_ledger
    except ImportError:
        from scripts.gate.ledger import load_ledger  # pragma: no cover
    try:
        ledger = load_ledger(input_data)
        epoch = _plan_mode_epoch(input_data, ledger)
        return ledger.get("plan_mode_notified_epoch") != epoch
    except Exception:
        return True


def mark_plan_mode_prompt_notified(input_data: dict[str, Any]) -> None:
    try:
        from ledger import load_ledger, save_ledger
    except ImportError:
        from scripts.gate.ledger import load_ledger, save_ledger  # pragma: no cover
    try:
        ledger = load_ledger(input_data)
        ledger["plan_mode_notified_epoch"] = _plan_mode_epoch(input_data, ledger)
        save_ledger(input_data, ledger)
    except Exception:
        pass


def pretool_should_append_plan_note(
    input_data: dict[str, Any],
    plan: dict[str, Any] | None,
) -> bool:
    """Skip PreToolUse plan footnote when UserPromptSubmit already sent it this epoch."""
    if not isinstance(plan, dict) or not plan.get("enabled"):
        return False
    try:
        from ledger import load_ledger
    except ImportError:
        from scripts.gate.ledger import load_ledger  # pragma: no cover
    try:
        ledger = load_ledger(input_data)
        epoch = _plan_mode_epoch(input_data, ledger)
        return ledger.get("plan_mode_notified_epoch") != epoch
    except Exception:
        return True
