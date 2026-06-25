#!/usr/bin/env python3
"""Stop-time completion handoff judge.

Detects when the agent ends a turn deferring autonomous work to the user
(permission-seeking questions, "say the word and I'll…", promised-but-unexecuted
follow-ups). Uses gpt-realtime-2 reasoning only — no regex or string matching
for deferral detection. Deterministic pre-filters (last turn had tools, empty
transcript) skip the judge call.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ledger import load_ledger, save_ledger
from transcript_tail import (
    TRANSCRIPT_TOKEN_BUDGET,
    fit_judge_user_message,
    stripped_transcript_tail,
)

# 0 (the default) disables the cap: the handoff judge never force-allows Stop on
# block count alone. Override via UNIFABLE_COMPLETION_HANDOFF_BLOCK_CAP for a
# finite bound.
try:
    COMPLETION_HANDOFF_BLOCK_CAP = int(os.environ.get("UNIFABLE_COMPLETION_HANDOFF_BLOCK_CAP", "0"))
except ValueError:
    COMPLETION_HANDOFF_BLOCK_CAP = 0

COMPLETION_HANDOFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok_to_stop": {
            "type": "boolean",
            "description": (
                "True only when the agent may end this turn: deliverable complete, "
                "or genuinely blocked on input only the user can provide."
            ),
        },
        "reason": {
            "type": "string",
            "description": "Quote transcript evidence; explain allow or block.",
        },
        "steering": {
            "type": "string",
            "description": ("When ok_to_stop is false: one concrete next action with tool calls. Empty when ok_to_stop is true."),
        },
        "blocked_on_user_only": {
            "type": "boolean",
            "description": (
                "True when stop is allowed because the agent needs user-only input "
                "(secrets, irreversible ops, genuine product choice)."
            ),
        },
    },
    "required": ["ok_to_stop", "reason"],
    "additionalProperties": False,
}

_JUDGE_SYSTEM = (
    "You are a strict completion-handoff monitor for an autonomous coding agent. "
    "The agent just ended a turn with text only (no tool calls in that final message). "
    "Decide whether it may stop or must continue and finish dangling work.\n\n"
    "Use semantic reasoning only. Do NOT pattern-match phrases; infer intent from context.\n\n"
    "BLOCK (ok_to_stop=false) when the last assistant message:\n"
    "- Asks permission for work the agent could do with available tools "
    '(e.g. "Want me to read/investigate/run…?", '
    '"If you want X, say the word and I\'ll run Y", '
    '"Should I proceed with the benchmark?").\n'
    "- Surfaces unresolved investigation, verification, analysis, or fixes the agent "
    "offered but did not execute.\n"
    "- Promises future autonomous work without acting in this turn.\n"
    "- Ends with optional next steps clearly in-scope for the current task that the "
    "agent could self-serve.\n\n"
    "ALLOW (ok_to_stop=true) when:\n"
    "- The user's request is fully delivered with no dangling autonomous work.\n"
    "- The agent is blocked on input ONLY the user can provide: secrets/credentials, "
    "irreversible or policy-sensitive actions (commit, push, deploy, force-push), "
    "or a genuine product/architecture choice between user-owned options.\n"
    "- The user asked a direct question and the agent gave a complete answer.\n"
    "- The final message is a status report with nothing left the agent could do "
    "without new user direction.\n\n"
    "When blocking, steering must name ONE concrete action (read that file, run that "
    "command, finish that investigation) — not 'ask the user again'.\n"
    "When allowing due to user-only blockage, set blocked_on_user_only=true."
)


def last_assistant_text_and_tool(transcript_path: str | None) -> tuple[str, bool]:
    """Return the last assistant turn's text and whether it included tool_use."""
    if not transcript_path:
        return "", False
    path = Path(transcript_path)
    if not path.is_file():
        return "", False

    last_text = ""
    last_had_tool = False
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message", obj)
            if obj.get("type") != "assistant" and msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            texts = [block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"]
            tools = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
            if texts or tools:
                last_text = "\n".join(texts).strip()
                last_had_tool = bool(tools)
    return last_text, last_had_tool


def _transcript_for_handoff_judge(transcript_path: str | None, input_data: dict[str, Any]) -> str:
    text = stripped_transcript_tail(transcript_path, TRANSCRIPT_TOKEN_BUDGET)
    if text.strip():
        return text
    last = input_data.get("last_assistant_message")
    if last:
        return f"assistant: {last}"
    return ""


def _recent_activity(ledger: dict[str, Any]) -> str:
    parts: list[str] = []
    for cmd in (ledger.get("ran_commands") or [])[-4:]:
        parts.append(f"ran: {cmd}")
    for path in (ledger.get("read_paths") or [])[-4:]:
        parts.append(f"read: {path}")
    return " | ".join(parts) if parts else "(none recorded)"


def judge_completion_handoff(
    transcript: str,
    *,
    user_goal: str = "",
    last_text: str = "",
    had_tool: bool = False,
    grade: str = "",
    recent_activity: str = "",
    judge: Any = None,
) -> dict[str, Any]:
    """Ask gpt-realtime-2 whether the agent may stop on this text-only turn."""
    if had_tool:
        return {
            "ok_to_stop": True,
            "reason": "last turn included tool calls",
            "steering": "",
            "blocked_on_user_only": False,
        }

    from judge_transport import ask_structured

    fn = judge or ask_structured
    question = {
        "user_goal": user_goal or "(unknown)",
        "grade": grade or "(unknown)",
        "last_assistant_text": last_text or "(empty)",
        "recent_activity": recent_activity or "(none)",
    }
    user = fit_judge_user_message(
        "Conversation transcript:\n",
        transcript or "(no transcript)",
        suffix=(
            "\n\nBased on the transcript and context above, may the agent end this turn?\n"
            f"QUESTION: {json.dumps(question, ensure_ascii=False)}"
        ),
    )
    return fn(
        _JUDGE_SYSTEM,
        user,
        COMPLETION_HANDOFF_SCHEMA,
        schema_name="completion_handoff",
    )


def completion_handoff_decision(input_data: dict[str, Any], cwd: str | Path) -> dict[str, Any] | None:
    """Return a Stop payload when handoff is unresolved, else None to allow."""
    if not input_data or input_data.get("_parse_error"):
        return None
    if not (input_data.get("session_id") or input_data.get("transcript_path") or input_data.get("last_assistant_message")):
        return None

    transcript_path = input_data.get("transcript_path")
    last_text, last_had_tool = last_assistant_text_and_tool(transcript_path)
    if not last_text and input_data.get("last_assistant_message"):
        last_text = str(input_data.get("last_assistant_message") or "").strip()
    if last_had_tool:
        return None
    if not last_text.strip():
        return None

    ledger = load_ledger(input_data)
    if COMPLETION_HANDOFF_BLOCK_CAP > 0 and int(ledger.get("completion_handoff_blocks") or 0) >= COMPLETION_HANDOFF_BLOCK_CAP:
        return {
            "systemMessage": ("Completion handoff block cap reached; allowing stop with a possibly unresolved handoff.")
        }

    user_goal = ""
    try:
        from spec_io import load_spec, resolve_session_id

        session_id = resolve_session_id(input_data, default=None)
        spec = load_spec(cwd, session_id)
        if spec:
            user_goal = str(spec.get("restated_goal") or "").strip()
    except Exception:
        pass

    grade = str(ledger.get("grade") or input_data.get("grade") or "").strip()
    transcript = _transcript_for_handoff_judge(transcript_path, input_data)
    recent = _recent_activity(ledger)

    try:
        verdict = judge_completion_handoff(
            transcript,
            user_goal=user_goal,
            last_text=last_text,
            had_tool=last_had_tool,
            grade=grade,
            recent_activity=recent,
        )
    except Exception:  # noqa: BLE001
        return None

    ok = verdict.get("ok_to_stop") is True
    reason = str(verdict.get("reason") or "").strip()
    steering = str(verdict.get("steering") or "").strip()

    if ok:
        ledger["completion_handoff_blocks"] = 0
        save_ledger(input_data, ledger)
        return None

    ledger["completion_handoff_blocks"] = int(ledger.get("completion_handoff_blocks") or 0) + 1
    save_ledger(input_data, ledger)

    alarm = "Stop blocked: unresolved handoff — do the offered work now."
    detail_parts = [alarm]
    if reason:
        detail_parts.append(reason)
    if steering:
        detail_parts.append(f"Next: {steering}")

    return {
        "decision": "block",
        "reason": "\n".join(detail_parts),
        "_handoff_steering": steering,
    }
