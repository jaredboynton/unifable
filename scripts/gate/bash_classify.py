#!/usr/bin/env python3
"""Classify whether a Bash command is allowed during the pre-spec research phase.

Whitelist by design: default BLOCK. Until a STANDARD+ task has a valid evidence
spec, Bash may run only `ls`, `glob`, `rg`, or a file whose basename is
`trace.sh`. The `trace.sh` exception exists so the explore skill can gather code
context without unlocking general shell access. Once a valid spec exists,
pre_tool_use.py skips this classifier and unlocks the normal action phase.
"""

from __future__ import annotations

import re
import shlex

ALLOWED_RESEARCH_BASH = "ls, glob, rg, or running any file named trace.sh"

_ALLOWED_COMMANDS = frozenset({"ls", "glob", "rg"})
_TRACE_INTERPRETERS = frozenset({"bash", "sh", "zsh"})
_WRAPPERS = frozenset({"sudo", "command", "env", "nice", "nohup", "time", "stdbuf"})
_SPLIT_RE = re.compile(r"\|\||&&|\||;|\n")
_ENVVAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _segments(command: str) -> list[str]:
    return [part.strip() for part in _SPLIT_RE.split(command) if part.strip()]


def _basename(token: str) -> str:
    return token.rstrip("/").rsplit("/", 1)[-1]


def _first_command(tokens: list[str]) -> tuple[str, list[str]]:
    idx = 0
    while idx < len(tokens):
        base = _basename(tokens[idx])
        if _ENVVAR_RE.match(tokens[idx]) or base in _WRAPPERS:
            idx += 1
            continue
        break
    if idx >= len(tokens):
        return "", []
    return tokens[idx], tokens[idx + 1:]


def _trace_target_from_interpreter(rest: list[str]) -> str:
    for token in rest:
        if token == "--":
            continue
        if token.startswith("-"):
            continue
        return token
    return ""


def _allowed_segment(seg: str) -> tuple[bool, str]:
    try:
        tokens = shlex.split(seg)
    except ValueError:
        tokens = seg.split()
    if not tokens:
        return False, "empty command"

    command, rest = _first_command(tokens)
    if not command:
        return False, "no executable command found"

    base = _basename(command)
    if base in _ALLOWED_COMMANDS:
        return True, ""
    if base == "trace.sh":
        return True, ""
    if base in _TRACE_INTERPRETERS and _basename(_trace_target_from_interpreter(rest)) == "trace.sh":
        return True, ""
    return False, f"{base} is not in the Bash research whitelist"


def is_allowed_research_bash(command: str) -> tuple[bool, str]:
    """Return (allowed, reason). reason is non-empty when blocked."""
    if not isinstance(command, str) or not command.strip():
        return False, "empty command"

    for seg in _segments(command):
        allowed, reason = _allowed_segment(seg)
        if not allowed:
            return False, reason
    return True, ""
