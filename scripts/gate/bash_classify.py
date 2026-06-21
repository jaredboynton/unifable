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

ALLOWED_RESEARCH_BASH = (
    "ls, glob, rg, running any file named trace.sh, or the append-only spec CLI "
    "(python3 scripts/gate/spec.py restate|add-task|cite|deliver|validate-task|dispute|status|validate|contract)"
)

_ALLOWED_COMMANDS = frozenset({"ls", "glob", "rg"})
_TRACE_INTERPRETERS = frozenset({"bash", "sh", "zsh"})
_PY_INTERPRETERS = frozenset({"python", "python3"})
# The agent may drive the evidence spec ONLY through these append-only subcommands.
# Creation is automatic (the gate_prompt hook), and removal is judge-only, so
# `create`/`init` and any `--force` are NOT here -- they would let the agent
# overwrite or wipe a spec. dispute records an impossibility claim (judge-adjudicated).
_SPEC_APPEND_SUBCMDS = frozenset({
    "restate", "add-task", "cite", "deliver", "validate-task", "dispute", "status", "validate", "contract",
})
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


def _spec_cli_segment(rest: list[str]) -> tuple[bool, str]:
    """Classify a `python[3] ...` segment that may invoke the gate's spec CLI.

    rest = tokens after the interpreter. Returns:
      (True, "")        -> an append-only scripts/gate/spec.py invocation: allow.
      (False, <reason>) -> it IS scripts/gate/spec.py but a forbidden subcommand
                           or carries --force: block with a specific reason.
      (False, "")       -> not the spec CLI at all: caller blocks generically.
    """
    # The script path is the first non-flag token after the interpreter.
    script = ""
    script_idx = -1
    for i, tok in enumerate(rest):
        if tok == "--" or tok.startswith("-"):
            continue
        script, script_idx = tok, i
        break
    if not script.replace("\\", "/").endswith("scripts/gate/spec.py"):
        return False, ""  # not the spec CLI

    if "--force" in rest:
        return False, ("spec.py --force is not allowed: the agent cannot overwrite or "
                       "remove a spec (creation is automatic, removal is judge-only).")
    # Subcommand = first non-flag token after the script path.
    sub = ""
    for tok in rest[script_idx + 1:]:
        if tok.startswith("-"):
            continue
        sub = tok
        break
    if sub in _SPEC_APPEND_SUBCMDS:
        return True, ""
    return False, (
        f"spec.py '{sub or '<none>'}' is not an append-only subcommand "
        "(creation is automatic, removal is judge-only). Allowed: "
        f"{', '.join(sorted(_SPEC_APPEND_SUBCMDS))}."
    )


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
    if base in _PY_INTERPRETERS:
        ok, reason = _spec_cli_segment(rest)
        if ok:
            return True, ""
        if reason:  # it is the spec CLI, but a forbidden invocation
            return False, reason
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
