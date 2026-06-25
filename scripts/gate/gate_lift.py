#!/usr/bin/env python3
"""Judge-granted evidence-gate lift (the director in control of the gate).

The deterministic evidence gate (hooks/pre_tool_use.py) blocks mutations while the
session is in the research phase (no validated spec) and the command is not on the
research whitelist. That is correct for unproven code work, but it can trap a
legitimate, explicitly-requested low-risk action (e.g. `cp a b` the user asked
for) when the task's evidence profile can never be satisfied by that action.

This module gives the director judge authority to LIFT that specific block. When
the gate is about to block a mutation, the hook calls `judge_gate_lift` on that
same PreToolUse call; an approved, scoped lift lets the tool run immediately, so
the main model never sees a block or retries. The lift is:

  - scoped: matched by an exact action signature (this tool + command/targets),
    so a DIFFERENT mutation is judged fresh rather than riding a stale grant;
  - bounded: a small per-grant use budget covers identical retries, and a
    per-session judge-call cap bounds runaway judging;
  - subordinate to the absolute guards: PROTECTED_PATHS (spec/.unifable, CLI-only),
    dangerous-env, and the spec-CLI-only rule are enforced UPSTREAM in the hook and
    are never reached here, so a lift can only open the research-phase block;
  - fail-closed to today's behavior: any judge error returns lift=0, leaving the
    existing block in place (never auto-allows on failure).

Host-agnostic: no imports from hooks/ or install/. State is read from / written to
a plain dict (the breaker state) by the caller; this module never does I/O.
"""

from __future__ import annotations

import json
import os
from typing import Any

GATE_LIFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lift": {
            "type": "integer",
            "enum": [0, 1],
            "description": (
                "1 to AUTHORIZE the imminent blocked mutation: it is a legitimate, "
                "low-risk, goal-aligned next step that the deterministic research-phase "
                "gate is wrongly blocking (e.g. a trivial file operation the user "
                "explicitly requested), and it does not rest on an unproven load-bearing "
                "claim. 0 to leave the block in place: the action is risky/destructive, "
                "unrelated to the user goal, or depends on an unverified assumption that "
                "should be grounded first."
            ),
        },
        "scope": {
            "type": "string",
            "description": (
                "One short clause naming exactly what is being authorized and why it is "
                "safe and goal-aligned (shown to the user). Empty when lift=0."
            ),
        },
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "The file path(s) this mutation may touch, as they appear in the command "
                "or transcript. Keep tightly scoped to the authorized action."
            ),
        },
    },
    "required": ["lift", "scope", "paths"],
    "additionalProperties": False,
}

_LIFT_SYSTEM = (
    "You are the unifable director, and you hold authority over the evidence gate "
    "for MUTATIONS. The deterministic gate has blocked an imminent tool call because "
    "the session has no validated evidence spec yet (research phase) and the command "
    "is not on the read-only research whitelist. Your job: decide whether to LIFT that "
    "block for THIS specific action.\n\n"
    "Lift (lift=1) when the action is a legitimate, low-risk, concrete step toward the "
    "user's goal that the gate is needlessly blocking -- most often a small, explicitly "
    "requested file operation (copy, move, create, rename) whose correctness is obvious "
    "and which rests on no unproven load-bearing claim. Do NOT lift (lift=0) when the "
    "action is destructive or wide-reaching (mass delete, history rewrite), is unrelated "
    "to the stated goal, or depends on an assumption that has not been grounded by tool "
    "output yet -- in those cases the agent should gather evidence first. You never need "
    "to consider protected unifable state or dangerous environment variables; those are "
    "enforced separately and are out of your scope. Keep `paths` tightly scoped to the "
    "authorized action."
)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def lift_uses_budget() -> int:
    """Identical-retry budget per grant (covers the action + a verify retry or two)."""
    return _int_env("UNIFABLE_GATE_LIFT_USES", 3)


def max_judge_calls() -> int:
    """Per-session cap on synchronous lift-judge calls (bounds runaway judging)."""
    return _int_env("UNIFABLE_GATE_LIFT_MAX_JUDGE", 8)


def action_signature(tool_name: str, command: str | None, paths: list[str] | None) -> str:
    """Stable signature for one mutation action. A different action -> different
    signature -> a fresh judge call rather than reusing a stale grant."""
    cmd = (command or "").strip()
    if cmd:
        return f"{tool_name}\x1f{cmd}"
    norm = sorted({str(p).strip() for p in (paths or []) if str(p).strip()})
    return f"{tool_name}\x1f" + "\x1e".join(norm)


def lift_from_state(state: Any) -> dict[str, Any]:
    """Read the persisted gate lift from breaker state. Safe default: {}."""
    if not isinstance(state, dict):
        return {}
    lift = state.get("breaker_gate_lift")
    return lift if isinstance(lift, dict) else {}


def lift_allows(lift: Any, tool_name: str, command: str | None, paths: list[str] | None) -> bool:
    """True when a persisted lift covers this exact action and has budget left.

    Exact-signature match only: the same blocked action retried (or its identical
    follow-up) reuses the grant; any different mutation returns False so the caller
    re-judges it. Fail-safe: malformed lift -> False (block stays)."""
    if not isinstance(lift, dict):
        return False
    try:
        if int(lift.get("uses") or 0) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    return str(lift.get("signature") or "") == action_signature(tool_name, command, paths)


def record_lift(
    state: dict[str, Any],
    tool_name: str,
    command: str | None,
    paths: list[str] | None,
    scope: str,
) -> None:
    """Persist a fresh scoped grant with a full use budget. Mutates state."""
    state["breaker_gate_lift"] = {
        "signature": action_signature(tool_name, command, paths),
        "tool": tool_name,
        "command": (command or "").strip(),
        "paths": [str(p).strip() for p in (paths or []) if str(p).strip()],
        "scope": str(scope or "").strip(),
        "uses": lift_uses_budget(),
    }


def consume_lift(state: dict[str, Any]) -> None:
    """Decrement the current grant's use budget by one. Mutates state."""
    lift = state.get("breaker_gate_lift")
    if isinstance(lift, dict):
        try:
            lift["uses"] = max(0, int(lift.get("uses") or 0) - 1)
        except (TypeError, ValueError):
            lift["uses"] = 0


def judge_budget_left(state: Any) -> bool:
    """True while the per-session synchronous lift-judge call cap is not exhausted."""
    if not isinstance(state, dict):
        return True
    try:
        return int(state.get("breaker_gate_lift_calls") or 0) < max_judge_calls()
    except (TypeError, ValueError):
        return True


def bump_judge_calls(state: dict[str, Any]) -> None:
    """Count one synchronous lift-judge call. Mutates state."""
    try:
        state["breaker_gate_lift_calls"] = int(state.get("breaker_gate_lift_calls") or 0) + 1
    except (TypeError, ValueError):
        state["breaker_gate_lift_calls"] = 1


def judge_gate_lift(
    *,
    goal: str,
    tool_name: str,
    command: str = "",
    paths: list[str] | None = None,
    transcript: str = "",
    board: str = "",
) -> dict[str, Any]:
    """Ask the judge whether to lift the evidence gate for this action.

    Returns the validated decision dict. Fail-closed: any judge error returns
    {"lift": 0, ...} so the existing block stays in place."""
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError:  # pragma: no cover
        return {"lift": 0, "scope": "", "paths": []}

    payload = {
        "user_goal": goal or "",
        "blocked_tool": tool_name,
        "blocked_command": command or "",
        "target_paths": [str(p) for p in (paths or [])],
    }
    if board:
        payload["spec_board"] = board
    user = json.dumps(payload, ensure_ascii=False)
    if transcript:
        user = f"{user}\n\nRecent transcript tail:\n{transcript}"
    try:
        res = ask_structured(_LIFT_SYSTEM, user, GATE_LIFT_SCHEMA, schema_name="gate_lift")
    except JudgeError:
        return {"lift": 0, "scope": "", "paths": []}
    if not isinstance(res, dict):
        return {"lift": 0, "scope": "", "paths": []}
    try:
        lift = 1 if int(res.get("lift", 0) or 0) == 1 else 0
    except (TypeError, ValueError):
        lift = 0
    out_paths = res.get("paths")
    return {
        "lift": lift,
        "scope": str(res.get("scope") or ""),
        "paths": [str(p) for p in out_paths] if isinstance(out_paths, list) else [],
    }
