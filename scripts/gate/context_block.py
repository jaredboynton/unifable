#!/usr/bin/env python3
"""Build the SessionStart context block.

Keep this startup frame short and imperative. It exists to prevent the first
avoidable block: the agent must restate the user goal through the append-only CLI
before trying normal work. Detailed workflow guidance belongs in later hook output.

Host-agnostic: no imports from hooks/ or install/. Fail-open by design.
"""

from __future__ import annotations

from pathlib import Path

_FALLBACK_INSPECTION_TOOLS = "Read, Grep, Glob, WebSearch, WebFetch, NotebookRead"
_FALLBACK_RESEARCH_BASH = "cd, ls, glob, rg, grep, read-only python/python3 -c, unifable spec CLI"


def _research_bash_summary() -> str:
    try:
        from tool_restrictions import bash_research_summary
    except ImportError:  # pragma: no cover
        try:
            from scripts.gate.tool_restrictions import bash_research_summary
        except Exception:
            return _FALLBACK_RESEARCH_BASH
    try:
        return bash_research_summary()
    except Exception:
        return _FALLBACK_RESEARCH_BASH


def _inspection_tools_summary() -> str:
    try:
        from tool_restrictions import inspection_tools_csv
    except ImportError:  # pragma: no cover
        try:
            from scripts.gate.tool_restrictions import inspection_tools_csv
        except Exception:
            return _FALLBACK_INSPECTION_TOOLS
    try:
        return inspection_tools_csv()
    except Exception:
        return _FALLBACK_INSPECTION_TOOLS


_FRAME_TEMPLATE = (
    "FIRST ACTION REQUIRED: your first tool call MUST run this CLI command:\n"
    "\n"
    "unifable restate '<goal in your own words>'\n"
    "\n"
    "Do this ONLY ONCE, before any other tool call. Until it succeeds, read-only inspection stays available, "
    "but write tools, delegation, and mutating Bash/REPL work stay blocked.\n"
    "\n"
    "Before the spec validates:\n"
    "- Inspection tools stay available: {inspection_tools}.\n"
    "- Bash/REPL/exec_command are limited to: {research_bash}.\n"
    "- Write tools (Edit, Write, MultiEdit, NotebookEdit, apply_patch) and "
    "delegation stay blocked unless a hook explicitly lifts them.\n"
    "\n"
    "Do not narrate exploration. Tool calls only until blocked or done.\n"
    "\n"
    "If a hook blocks you, follow its exact instruction next."
)


def build_session_context(plugin_root: str | Path | None = None) -> str:
    """Return the standing SessionStart context string.

    plugin_root is accepted but unused so the signature can grow without changing
    call sites.
    """
    return _FRAME_TEMPLATE.format(
        inspection_tools=_inspection_tools_summary(),
        research_bash=_research_bash_summary(),
    )


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
