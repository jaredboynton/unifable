#!/usr/bin/env python3
"""Deterministic per-step tool scope (stepwise harness).

The director judge (groundedness.py) persists a tool scope and a minimal directive
into breaker state on each debounced judge call. This module is the cheap,
JUDGE-FREE predicate the PreToolUse hook runs on EVERY tool call to decide whether
an imminent tool is in scope. No I/O, no judge, no network -- pure data.

Scope shape (all keys optional):
    {
        "allow": ["Read", "Bash", ...],   # if non-empty, ONLY these tools pass
        "deny":  ["Edit", "Bash", ...],    # these tools are blocked
        "directive": "<minimal next-step instruction shown to the model>",
    }

Invariants (fail-safe by construction):
  - Empty / missing / malformed scope allows everything (fail-open). A director
    bug or an unreachable judge must never restrict the agent.
  - The GROUNDING FLOOR (Read/Grep/Glob/WebSearch/WebFetch/NotebookRead) is NEVER
    blocked, regardless of scope. The agent can always ground a claim and read its
    way out of a bad scope -- mirrors the groundedness breaker's "reads always
    free" invariant, so a scope can steer mutations but can never brick the
    session.

Host-agnostic: no imports from hooks/ or install/.
"""

from __future__ import annotations

from typing import Any

# Read-only tools that stay reachable under any scope so the agent can always
# ground a claim. Kept in sync with groundedness.RELEASE_TOOLS.
GROUNDING_FLOOR = frozenset(
    {"Read", "Grep", "Glob", "WebSearch", "WebFetch", "NotebookRead"}
)

_DEFAULT_BLOCK_REASON = (
    "That tool is out of scope for the current step. "
    "Follow the judge's directive for what to do next."
)


def _str_list(value: Any) -> list[str] | None:
    """Return value as a list of tool-name strings, or None if not a clean list."""
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        out.append(item)
    return out


def scope_from_state(state: Any) -> dict[str, Any]:
    """Read the persisted tool scope from breaker state. Safe default: {}."""
    if not isinstance(state, dict):
        return {}
    scope = state.get("breaker_tool_scope")
    return scope if isinstance(scope, dict) else {}


def current_directive(state: Any) -> str:
    """Read the persisted minimal directive from breaker state. Default: ''."""
    if not isinstance(state, dict):
        return ""
    return str(state.get("breaker_directive") or "")


def in_scope(tool_name: str, scope: Any) -> tuple[bool, str]:
    """Return (allowed, reason).

    allowed=True with reason="" when the tool may run. allowed=False with a
    non-empty reason (the director's directive, else a default) when the scope
    blocks the tool. Fail-open on any malformed input.
    """
    tool = str(tool_name or "")
    # Grounding floor: never blocked.
    if tool in GROUNDING_FLOOR:
        return True, ""
    if not isinstance(scope, dict) or not scope:
        return True, ""

    directive = str(scope.get("directive") or "").strip()
    reason = directive or _DEFAULT_BLOCK_REASON

    deny = _str_list(scope.get("deny"))
    if deny and tool in deny:
        return False, reason

    allow = _str_list(scope.get("allow"))
    if allow:  # non-empty allow-list is exclusive
        if tool not in allow:
            return False, reason

    return True, ""
