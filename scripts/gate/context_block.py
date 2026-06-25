#!/usr/bin/env python3
"""Build the SessionStart context block that replaces the old static
CLAUDE.md/AGENTS.md operating-mode injection.

This is the standing, once-per-session posture. Per-prompt concerns (grade
context, effort playbook, spec scaffold tutorial, router packs, heavy-workflow
brief) are delivered by the UserPromptSubmit / PreToolUse / Stop hooks and are
NOT duplicated here.

Host-agnostic: no imports from hooks/ or install/. Fail-open by design.
"""

from __future__ import annotations

from pathlib import Path

try:
    from research_bash_guidance import explore_trace_inline_md, explore_trace_list_item_md
except Exception:  # noqa: BLE001 -- fail open

    def explore_trace_inline_md() -> str:
        return ""

    def explore_trace_list_item_md() -> str:
        return ""


def _explore_inline() -> str:
    try:
        return explore_trace_inline_md()
    except Exception:  # noqa: BLE001
        return ""


def _explore_list() -> str:
    try:
        return explore_trace_list_item_md()
    except Exception:  # noqa: BLE001
        return ""


_HEADER = (
    "unifable operating mode (auto-route by task signal; apply what the task signals, baseline only when there is no signal)."
)

_ALWAYS = (
    "- Lead with the outcome. Stay within the requested scope (no incidental "
    "refactors). Cite evidence for every load-bearing claim: path:line for code, "
    "command -> output for tool results, a source URL for research/prior art; "
    "label anything uncited (assumption) -- this is never traded away for brevity. "
    "Confirm before destructive or hard-to-reverse actions."
)

_GATE_POINTER = (
    "- Evidence gate (always on, no disable): until the spec for the current task "
    "validates, it blocks edits, Task/Agent delegation, Bash outside the research "
    "whitelist (cd, ls, glob, rg, head/wc/tail/sort/uniq, read-only git, "
    "git workflow status/add/commit/push "
    "(no --force){explore_list}, unifusion scripts, or the append-only spec CLI "
    "unifable restate|add-task|set-primary|add-frontier|dispute), AND "
    "completion. The spec is seeded automatically; FIRST run "
    "`unifable restate '<your words>'`, then `unifable add-task --title ... --check ...`. "
    "Citations sync from your reads/fetches automatically (code profile only). "
    "quick/LIGHT tasks are waived. Assumptions never satisfy the gate."
)

_HOOK_BLOCK = (
    "- When a hook blocks a tool, treat the hook message as the current instruction. "
    "Do not retry the same blocked tool, wait out a debounce, or use Bash for sleep/echo "
    "scaffolding. If the groundedness breaker flags a claim, retract it in one sentence "
    "if it is no longer load-bearing, or ground it with a read-only action (Read/Grep/"
    "Glob/WebSearch/WebFetch or allowed research Bash) and cite what you actually read "
    "before retrying. If the evidence-spec gate blocks Bash, use the unifable spec CLI; "
    "before validation, every Bash segment must match the research whitelist above "
    "({explore_inline}unifusion scripts, or unifable)."
)

_EDIT_DISCIPLINE = (
    "- Every edit and self-review: anchor find-and-replace on \\bword\\b, then grep for "
    "malformed compounds after the pass. Before flagging a problem in self-review, confirm "
    "it with a tool call -- absence of evidence is not a finding. Never weaken or delete a "
    "test to make it pass. Accumulate minor concerns; halt and surface all at once on the third."
)

_FINAL_RESPONSE = (
    "- Final response shape by depth: quick = 1-3 lines + next step; "
    "normal = outcome + brief evidence + next step; "
    "deep = outcome + evidence + one-line verification + next step. "
    "Lead with the outcome; do not narrate internal reasoning."
)

_ORCH_POSTURE = (
    "- Orchestrator posture: default to delegating non-trivial work to subagents (in "
    "parallel when the work is independent). You plan, hand each worker a distilled brief "
    "plus a strict output contract, validate via deterministic gates, and synthesize -- "
    "you do not grind heavy work in the main thread. For a simple question, a single-file "
    "fix, or a one-step task, answer directly; do not over-orchestrate. "
    "Do not trust training data: designs, plans, fixes, and solutions must cite current "
    "documentation or research corroborated by a repo or document URL."
)

_RESEARCH_REFLEXES = (
    "- Avoid two common research walls. (1) Bash synonyms: rg/grep/ast-grep, glob/ls not find, "
    "head/wc/tail not cat, no pwd; python3 -c unlocks post-spec only. "
    "(2) Read before naming locations -- citing unread paths arms the breaker."
)

_RESEARCH_DELEGATION = (
    "- Research phase = no subagents: Task/Agent delegation is BLOCKED by the evidence "
    "gate until the spec validates (both Claude and Codex). Do NOT spawn subagents to "
    "explore -- they will be blocked, and waiting on them deadlocks. Favor skills over "
    "agents for exploration: use the explore skill's `trace.sh` for codebase tracing and "
    "`websearch.sh` for rapid external research (docs, papers, GitHub prior art); both are "
    "in the Bash research whitelist. Delegate to subagents only after the gate lifts."
)


def build_session_context(plugin_root: str | Path | None = None) -> str:
    """Return the standing SessionStart context string.

    plugin_root is unused for now but accepted so the signature can grow
    (e.g. goals.py path injection) without changing call sites.
    """
    explore_inline = _explore_inline()
    explore_list = _explore_list()

    gate = _GATE_POINTER.format(explore_list=explore_list)
    hook_block = _HOOK_BLOCK.format(explore_inline=explore_inline)

    parts = [
        _HEADER,
        "",
        _ALWAYS,
        gate,
        hook_block,
        _EDIT_DISCIPLINE,
        _FINAL_RESPONSE,
        _ORCH_POSTURE,
        _RESEARCH_REFLEXES,
        _RESEARCH_DELEGATION,
    ]
    return "\n".join(parts)


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
