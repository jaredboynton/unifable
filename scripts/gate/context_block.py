#!/usr/bin/env python3
"""Build the SessionStart context block — the THIN judge-relationship frame.

Stepwise harness: the model is no longer front-loaded with the full operating-mode
posture. This frame only (1) tells the model it is working under a per-tool director
judge that opens/restricts its tools and tends the goal spec, and (2) instructs it
to restate the goal first. All step-by-step guidance (what to do next, which tools
are open, citation/edit discipline) is delivered at runtime by the director judge
on each tool call, not front-loaded here.

Host-agnostic: no imports from hooks/ or install/. Fail-open by design.
"""

from __future__ import annotations

from pathlib import Path

_FRAME = (
    "unifable operating mode (stepwise, judge-driven).\n"
    "\n"
    "- You are working with a judge agent that guides you step by step. On each "
    "action it opens or restricts your tools to keep you on evidence-backed, "
    "thoroughly-researched approaches, and it tends a goal spec on your behalf -- "
    "marking tasks complete and adding new ones as the work clarifies.\n"
    "- The judge tells you exactly what you may and may not do next. When a hook "
    "message appears, treat it as your current instruction: follow it instead of "
    "retrying the blocked action or working around it.\n"
    "- Start by restating the user goal in your own words, then let the judge guide "
    "the rest. Drive the spec only through the append-only CLI (never edit the JSON):\n"
    "    - FIRST: unifable restate '<the intended outcome, in your own words>'  "
    "(the gate stays blocked until you restate)\n"
    "    - then add the first requirement: unifable add-task --title '<requirement>' "
    "--check '<runnable check>'\n"
    "- Cite evidence the judge can check; assumptions never satisfy the gate."
)


def build_session_context(plugin_root: str | Path | None = None) -> str:
    """Return the standing SessionStart context string (the thin frame).

    plugin_root is accepted but unused so the signature can grow without changing
    call sites.
    """
    return _FRAME


def build_session_payload(plugin_root: str | Path | None = None) -> dict:
    """Return the SessionStart hookSpecificOutput payload."""
    try:
        context = build_session_context(plugin_root=plugin_root)
    except Exception:  # noqa: BLE001 -- fail open, never block session start
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
