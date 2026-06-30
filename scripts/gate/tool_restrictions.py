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
SHELL_TOOLS = ("Bash", "REPL", "exec_command", "Shell")
MCP_TOOL_MATCHER = r"mcp__.*"
MCP_MUTATION_TOOLS_LABEL = (
    "MCP mutation tools (mcp__* except read/get/fetch/content/search/file/view/lookup/list/query unless payload is write-like)"
)

PRETOOL_GATED_TOOLS = SHELL_TOOLS + DELEGATION_TOOLS + WRITE_TOOLS
GROUNDEDNESS_BLOCKED_TOOLS = WRITE_TOOLS + SHELL_TOOLS
RELEASE_TOOLS = INSPECTION_TOOLS + ("view_image",)

_MCP_WRITE_RE = re.compile(
    r"(?i)(write|create|update|delete|patch|apply|remove|insert|put|post|send|upload|modify|mutate|replace|move|upsert|purge|clear|destroy|rename|copy)"
)
_MCP_READ_RE = re.compile(r"(?i)(read|get|fetch|content|search|file|view|lookup|list|query)")
_MCP_WRITE_INPUT_KEYS = frozenset(
    {
        "body",
        "content",
        "contents",
        "patch",
        "diff",
        "old_string",
        "new_string",
        "replacement",
        "payload",
        "value",
        "data",
        "bytes",
        "script",
    }
)
_MCP_QUERY_KEYS = frozenset({"query", "sql", "statement", "graphql"})
_MCP_QUERY_MUTATION_RE = re.compile(
    r"(?is)^\s*(?:"
    r"mutation\b|"
    r"insert\b|update\b|delete\b|drop\b|alter\b|create\b|truncate\b|merge\b|replace\b|grant\b|revoke\b"
    r")"
)

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
    return tool_csv(GROUNDEDNESS_BLOCKED_TOOLS) + f", {MCP_MUTATION_TOOLS_LABEL}"


def pretool_matcher_regex() -> str:
    exact = "|".join(re.escape(tool) for tool in PRETOOL_GATED_TOOLS)
    return f"^({exact}|{MCP_TOOL_MATCHER})$"


def is_mcp_tool_name(tool_name: str) -> bool:
    return str(tool_name or "").startswith("mcp__")


def is_mcp_read_like_tool(tool_name: str) -> bool:
    name = str(tool_name or "")
    if not is_mcp_tool_name(name):
        return False
    if _MCP_WRITE_RE.search(name):
        return False
    return bool(_MCP_READ_RE.search(name))


def is_mcp_mutation_tool(tool_name: str) -> bool:
    name = str(tool_name or "")
    return is_mcp_tool_name(name) and not is_mcp_read_like_tool(name)


def mcp_input_forces_mutation(tool_input) -> bool:
    """Conservative structural override for read-like MCP names carrying write payloads."""

    def walk(value) -> bool:
        if isinstance(value, dict):
            lowered = {str(k).lower(): v for k, v in value.items()}
            if lowered.get("destructivehint") is True or lowered.get("destructive") is True:
                return True
            if any(key in lowered for key in _MCP_WRITE_INPUT_KEYS):
                return True
            for key in _MCP_QUERY_KEYS:
                query = lowered.get(key)
                if isinstance(query, str) and _MCP_QUERY_MUTATION_RE.search(query):
                    return True
            return any(walk(v) for v in lowered.values())
        if isinstance(value, list):
            return any(walk(item) for item in value)
        return False

    return walk(tool_input)


def is_pretool_gated_tool(tool_name: str) -> bool:
    name = str(tool_name or "")
    return name in PRETOOL_GATED_TOOLS or is_mcp_tool_name(name)


def is_groundedness_blocked_tool(tool_name: str) -> bool:
    name = str(tool_name or "")
    return name in GROUNDEDNESS_BLOCKED_TOOLS or is_mcp_mutation_tool(name)


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
