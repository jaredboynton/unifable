#!/usr/bin/env python3
"""Canonical hook-visible tool restriction copy.

Host-agnostic: this module owns the user-facing lists of tools and research
commands shown by hooks. Judge prompts should refer to evidence needs, not
hand-authored tool inventories.
"""

from __future__ import annotations

import re

try:
    from research_bash_guidance import bash_allowed_summary
except ImportError:  # pragma: no cover
    from scripts.gate.research_bash_guidance import bash_allowed_summary


INSPECTION_TOOLS = ("Read", "Grep", "Glob", "WebSearch", "WebFetch", "NotebookRead")
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch")
DELEGATION_TOOLS = ("Task", "Agent")
SHELL_TOOLS = ("Bash", "REPL", "exec_command")

PRETOOL_GATED_TOOLS = SHELL_TOOLS + DELEGATION_TOOLS + WRITE_TOOLS
GROUNDEDNESS_BLOCKED_TOOLS = WRITE_TOOLS + SHELL_TOOLS
RELEASE_TOOLS = INSPECTION_TOOLS + ("view_image",)

_FOOTER_TITLE = "Actions restricted to:"

_LEGACY_GROUNDEDNESS_RESTRICTION_RE = re.compile(
    r"(?is)\b(?:"
    r"your\s+tools\s+are\s+restricted\s+to|"
    r"tools\s+are\s+restricted\s+to|"
    r"restrict\s+tools\s+to"
    r")\s+read-only\s+(?:ones\s*)?\([^)]*\)\s+and\s+whitelisted\s+research\s+Bash"
    r"\b.*?\buntil\s+"
    r"(?:you\s+ground\s+the\s+claim|it\s+grounds\s+the\s+claim|this\s+is\s+grounded|grounded)"
    r"\.?"
)


def tool_csv(tools: tuple[str, ...] | frozenset[str]) -> str:
    return ", ".join(tools)


def inspection_tools_csv() -> str:
    return tool_csv(INSPECTION_TOOLS)


def write_tools_csv() -> str:
    return tool_csv(WRITE_TOOLS)


def delegation_tools_csv() -> str:
    return tool_csv(DELEGATION_TOOLS)


def shell_tools_csv() -> str:
    return tool_csv(SHELL_TOOLS)


def groundedness_blocked_tools_csv() -> str:
    return tool_csv(GROUNDEDNESS_BLOCKED_TOOLS)


def pretool_matcher_regex() -> str:
    return "^(" + "|".join(re.escape(tool) for tool in PRETOOL_GATED_TOOLS) + ")$"


def bash_research_summary() -> str:
    return bash_allowed_summary()


def groundedness_restriction_footer() -> str:
    return "\n".join(
        (
            _FOOTER_TITLE,
            f"- Available inspection tools: {inspection_tools_csv()}.",
            f"- Shell/REPL tools ({shell_tools_csv()}): {bash_research_summary()}.",
            f"- Blocked until grounded: {groundedness_blocked_tools_csv()}.",
        )
    )


def strip_legacy_groundedness_restrictions(message: str) -> str:
    stripped = _LEGACY_GROUNDEDNESS_RESTRICTION_RE.sub("", str(message or ""))
    stripped = re.sub(r"[ \t]+", " ", stripped)
    stripped = re.sub(r"\s+\.", ".", stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def groundedness_block_message(steering: str) -> str:
    message = strip_legacy_groundedness_restrictions(steering)
    if not message:
        message = "You asserted a load-bearing claim without evidence. Ground the claim before mutating."
    footer = groundedness_restriction_footer()
    if _FOOTER_TITLE in message:
        return message
    return f"{message}\n\n{footer}"
