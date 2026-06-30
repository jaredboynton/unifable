#!/usr/bin/env python3
"""Deterministic self-contradiction detector for the completion gate.

A completion-gate task check that requires an action the research-phase shell
allowlist blocks is a GATE SELF-CONTRADICTION: the agent can never satisfy the
check without running a command the gate refuses, so the completion breaker loops
forever waiting for an async loop-release judge that may never fire (the original
access-token-migration deadlock: a task check asserting `branch --show-current !=
main` while `git checkout`/`git switch` were blocked).

Fix A made non-destructive branch switching research-phase-safe, so the specific
deadlock is gone. This module guards the GENERAL class: a task check that runs a
still-blocked git subcommand (reset/merge/rebase/restore/clean/...) or a
destructive checkout/switch shape (pathspec, --detach, --force, --ours/--theirs,
--merge, --patch, --discard-changes). When detected at Stop, the completion gate
appends a concrete, judge-independent notice to the block reason naming the
blocked action and the allowed alternative -- so the agent has an immediate,
deterministic escape (revise the check, or restate the goal) instead of an
infinite judge loop.

Host-agnostic, pure-function, fail-closed: returns "" on any error so a detector
bug never blocks or changes gate behavior.
"""

from __future__ import annotations

import re

try:
    from bash_classify import _BLOCKED_GIT_SUBCMDS, _GIT_NAV_SUBCMDS
except ImportError:  # pragma: no cover
    from scripts.gate.bash_classify import _BLOCKED_GIT_SUBCMDS, _GIT_NAV_SUBCMDS

# A task check that runs a still-blocked git subcommand (everything in
# _BLOCKED_GIT_SUBCMDS EXCEPT the non-destructive nav carve-out from Fix A).
_BLOCKED_NON_NAV = sorted(s for s in _BLOCKED_GIT_SUBCMDS if s not in _GIT_NAV_SUBCMDS)
_BLOCKED_GIT_IN_CHECK_RE = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?(" + "|".join(_BLOCKED_NON_NAV) + r")\b",
    re.I,
)
# Destructive checkout/switch shapes that stay blocked even after Fix A.
_DESTRUCTIVE_NAV_IN_CHECK_RE = re.compile(
    r"\bgit\s+(?:-C\s+\S+\s+)?(?:checkout|switch)\b[^;\n&|]*"
    r"(?:--detach|--force|-f\b|--ours|--theirs|--merge|-m\b|--patch|-p\b|--discard-changes|\s--\s|\s\.\s)",
    re.I,
)


def _task_check_requires_blocked_action(check: str) -> str | None:
    """Return a short description of the blocked action the check needs, or None."""
    m = _BLOCKED_GIT_IN_CHECK_RE.search(check)
    if m:
        return f"git {m.group(1)} (blocked in the research phase)"
    if _DESTRUCTIVE_NAV_IN_CHECK_RE.search(check):
        return (
            "a destructive checkout/switch shape "
            "(--detach/--force/--ours/--theirs/--merge/--patch/--discard-changes/pathspec)"
        )
    return None


def detect_self_contradiction(spec: dict, incomplete_ids) -> str:
    """Return a deterministic notice if any incomplete task's check requires an
    action the research-phase shell allowlist blocks; else "".

    `incomplete_ids` is the list of task-id strings the completion gate could not
    validate this Stop. The notice is appended to the block reason so the agent
    gets a judge-independent next step; it never passes a task silently."""
    try:
        incomplete = set(str(t) for t in (incomplete_ids or []))
        tasks = (spec or {}).get("tasks") or []
        hits: list[str] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            tid = str(task.get("id") or "")
            if tid not in incomplete:
                continue
            check = str(task.get("check") or "")
            blocked = _task_check_requires_blocked_action(check)
            if blocked:
                hits.append(f"- {tid}: check requires {blocked}")
        if not hits:
            return ""
        return (
            "Gate self-contradiction (deterministic): incomplete task checks require an "
            "action the research-phase shell allowlist blocks, so they can never pass "
            "while the spec is unvalidated. Revise each check to verify the OUTCOME "
            "without the blocked action (e.g. `git show-ref --verify refs/heads/<name>` "
            "for a branch ref, or `git rev-parse --abbrev-ref HEAD` after a now-allowed "
            "`git checkout|switch <branch>`), or restate the goal so the blocked action "
            "is part of the validated work:\n" + "\n".join(hits)
        )
    except Exception:
        return ""
