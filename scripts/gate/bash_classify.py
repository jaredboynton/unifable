#!/usr/bin/env python3
"""Classify whether a Bash command is allowed during the pre-spec research phase.

Whitelist by design: default BLOCK. Until a STANDARD+ task has a valid evidence
spec, Bash may run only `cd`, `ls`, `glob`, `rg`, `grep`/`egrep`/`fgrep`, `echo` (read-only pipeline sinks only),
`ast-grep`/`sg`, read-only file inspection
(`head`, `tail`, `wc`, `sort`, `uniq`), read-only `git` subcommands, git
workflow commands (`status`, `add`, `commit`, `push` without `--force`), a file
whose basename is `trace.sh` or `websearch.sh` when the explore skill is
installed (guidance shows resolved paths), or a file whose basename is one of
the user-facing unifusion skill scripts (panel research). Once a valid spec
exists, pre_tool_use.py skips this classifier and unlocks the normal action phase.
"""

from __future__ import annotations

import re
import shlex

try:
    from research_bash_guidance import EXPLORE_SCRIPT_BASENAMES, allowed_research_bash_detail
except ImportError:  # pragma: no cover
    from scripts.gate.research_bash_guidance import EXPLORE_SCRIPT_BASENAMES, allowed_research_bash_detail

ALLOWED_RESEARCH_BASH = allowed_research_bash_detail()

_ALLOWED_COMMANDS = frozenset({"cd", "ls", "glob", "rg", "grep", "egrep", "fgrep", "echo"})
_AST_GREP_NAMES = frozenset({"ast-grep", "sg"})
_AST_GREP_REWRITE_FLAGS = frozenset({"-U", "--update", "--rewrite"})
# Standalone or as pipeline sinks after an allowed command.
READONLY_INSPECTION_COMMANDS = frozenset({"head", "tail", "wc", "sort", "uniq"})
_PIPELINE_SINKS = READONLY_INSPECTION_COMMANDS
_TRACE_INTERPRETERS = frozenset({"bash", "sh", "zsh"})
_UNIFUSION_SCRIPT_NAMES = frozenset(
    {
        "unifusion.sh",
        "save_run.sh",
        "summarize_session.sh",
        "resolve_session.sh",
    }
)
_PY_INTERPRETERS = frozenset({"python", "python3"})
# The agent may drive the evidence spec ONLY through these append-only subcommands.
# Creation is automatic (the gate_prompt hook), and removal is judge-only, so
# `create`/`init` and any `--force` are NOT here -- they would let the agent
# overwrite or wipe a spec. dispute records an impossibility claim (judge-adjudicated).
_SPEC_APPEND_SUBCMDS = frozenset({"restate", "add-task", "set-primary", "add-frontier", "dispute"})
_SPEC_CLI_NAMES = frozenset({"unifable", "unifable-spec"})
_WRAPPERS = frozenset({"sudo", "command", "env", "nice", "nohup", "time", "stdbuf"})
_ENVVAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ASSIGN_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
_AGENT_BLOCKED_ASSIGN_NAMES = frozenset({"UNIFABLE_DEV"})
# Declaration builtins that take NAME=VALUE assignments. A standalone segment made
# only of these (e.g. `T=/long/path` or `export A=1 B=2`) carries no executable but
# is a harmless way to name a value for reuse in a later whitelisted segment.
_SAFE_DECL_PREFIXES = frozenset({"export"})
# Assigning these can change which binary the shell resolves or how words split,
# so a standalone declaration of them is NOT a no-op and stays blocked.
_DANGEROUS_ASSIGN_NAMES = frozenset(
    {
        "PATH",
        "IFS",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "PS4",
        "GLOBIGNORE",
        "CDPATH",
    }
)
# Git subcommands allowed before the evidence spec validates.
_ALLOWED_GIT_SUBCMDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "rev-parse",
        "describe",
        "branch",
        "remote",
        "tag",
        "stash",
        "blame",
        "shortlog",
        "reflog",
        "merge-base",
        "name-rev",
        "ls-remote",
        "ls-files",
        "ls-tree",
        "cat-file",
        "for-each-ref",
        "show-ref",
        "rev-list",
        "grep",
        "check-ignore",
        "check-attr",
        "check-ref-format",
        "check-mailmap",
        "verify-commit",
        "verify-tag",
        "help",
        "archive",
        "count-objects",
        "merge-tree",
        "whatchanged",
        "diff-tree",
        "get-tar-commit-id",
        "var",
        "config",
        "add",
        "commit",
        "push",
    }
)
_BLOCKED_GIT_SUBCMDS = frozenset(
    {
        "pull",
        "fetch",
        "checkout",
        "switch",
        "restore",
        "reset",
        "revert",
        "merge",
        "rebase",
        "cherry-pick",
        "clean",
        "rm",
        "mv",
        "init",
        "clone",
        "apply",
        "am",
        "bisect",
        "bundle",
        "filter-branch",
        "gc",
        "maintenance",
        "notes",
        "receive-pack",
        "send-pack",
        "submodule",
        "update-index",
        "update-ref",
        "worktree",
    }
)
# Global git options that take a value and must be skipped before the subcommand.
_GIT_GLOBAL_OPTS_WITH_VALUE = frozenset(
    {
        "-C",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--super-prefix",
    }
)
_GIT_GLOBAL_OPTS = frozenset(
    {
        "-C",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--exec-path",
        "--super-prefix",
        "--no-pager",
        "--no-optional-locks",
        "-c",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--no-replace-objects",
    }
)


def _logical_lines(command: str) -> list[str]:
    """Split on newlines, joining backslash-continued lines into one logical line."""
    lines: list[str] = []
    buf = ""
    for raw in command.splitlines():
        part = raw.strip()
        if not part and not buf:
            continue
        if buf:
            buf += " " + part
        else:
            buf = part
        if buf.endswith("\\"):
            buf = buf[:-1].rstrip()
            continue
        lines.append(buf)
        buf = ""
    if buf:
        lines.append(buf)
    return lines


def _join_flag_lines(lines: list[str]) -> list[str]:
    """Join lines that are only flags onto the preceding command (multiline cite)."""
    joined: list[str] = []
    for line in lines:
        if line.startswith("-") and joined:
            joined[-1] = joined[-1] + " " + line
        else:
            joined.append(line)
    return joined


def _split_outside_quotes(segment: str, *, pipe: bool, compound: bool) -> list[str]:
    """Split on shell operators outside quoted strings."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(segment)
    in_single = False
    in_double = False
    while i < n:
        ch = segment[i]
        if in_single:
            buf.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(segment[i + 1])
                i += 2
                continue
            if ch == '"':
                in_double = False
            i += 1
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        if compound and (segment.startswith("&&", i) or segment.startswith("||", i)):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if compound and ch == ";":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        if pipe and ch == "|":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _split_compound(segment: str) -> list[str]:
    return _split_outside_quotes(segment, pipe=False, compound=True)


def _split_pipes(segment: str) -> list[str]:
    return _split_outside_quotes(segment, pipe=True, compound=False)


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
    return tokens[idx], tokens[idx + 1 :]


def _agent_blocked_assignment_reason(tokens: list[str]) -> str:
    for idx, tok in enumerate(tokens):
        match = _ASSIGN_NAME_RE.match(tok)
        if match and match.group(1) in _AGENT_BLOCKED_ASSIGN_NAMES:
            return f"{match.group(1)} is reserved for operator diagnostics and cannot be set from agent Bash"

        base = _basename(tok)
        if base in ("export", "declare", "typeset", "local", "readonly"):
            for arg in tokens[idx + 1 :]:
                if arg.startswith("-"):
                    continue
                match = _ASSIGN_NAME_RE.match(arg)
                if match and match.group(1) in _AGENT_BLOCKED_ASSIGN_NAMES:
                    return f"{match.group(1)} is reserved for operator diagnostics and cannot be set from agent Bash"
            return ""
        if base == "unset":
            for arg in tokens[idx + 1 :]:
                if arg.startswith("-"):
                    continue
                if arg in _AGENT_BLOCKED_ASSIGN_NAMES:
                    return f"{arg} is reserved for operator diagnostics and cannot be changed from agent Bash"
            return ""
    return ""


def blocked_agent_env_reason(command: object) -> str:
    if not isinstance(command, str) or not command.strip():
        return ""
    try:
        segments = _split_compound(command)
    except Exception:
        segments = [command]
    for segment in segments:
        try:
            pipe_parts = _split_pipes(segment)
        except Exception:
            pipe_parts = [segment]
        for part in pipe_parts:
            try:
                tokens = shlex.split(part)
            except ValueError:
                tokens = part.split()
            reason = _agent_blocked_assignment_reason(tokens)
            if reason:
                return reason
    return ""


def _trace_target_from_interpreter(rest: list[str]) -> str:
    for token in rest:
        if token == "--":
            continue
        if token.startswith("-"):
            continue
        return token
    return ""


def _is_unifusion_script(token: str) -> bool:
    return _basename(token) in _UNIFUSION_SCRIPT_NAMES


def _validate_spec_append_args(args: list[str]) -> tuple[bool, str]:
    """Validate append-only subcommands for unifable-spec or scripts/gate/spec.py."""
    if "--force" in args:
        return False, (
            "spec CLI --force is not allowed: the agent cannot overwrite or "
            "remove a spec (creation is automatic, removal is judge-only)."
        )
    sub = ""
    for tok in args:
        if tok.startswith("-"):
            continue
        sub = tok
        break
    if sub in _SPEC_APPEND_SUBCMDS:
        if sub == "restate" and any(tok == "--goal" or tok.startswith("--goal=") for tok in args):
            return False, ("restate uses a positional goal: unifable restate '<goal>' (not --goal).")
        return True, ""
    return False, (
        f"spec CLI '{sub or '<none>'}' is not an append-only subcommand "
        "(creation is automatic, removal is judge-only). Allowed: "
        f"{', '.join(sorted(_SPEC_APPEND_SUBCMDS))}."
    )


def _git_subcommand(rest: list[str]) -> str:
    """Return the first git subcommand token after global options."""
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in _GIT_GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-") or tok in _GIT_GLOBAL_OPTS:
            i += 1
            continue
        return tok
    return ""


def _validate_ast_grep_readonly(rest: list[str]) -> tuple[bool, str]:
    """Allow ast-grep scan/run/test; block in-place rewrite flags."""
    for tok in rest:
        if tok in _AST_GREP_REWRITE_FLAGS or tok.startswith("--update="):
            return False, "ast-grep file rewrite is not allowed before the evidence spec is validated"
    return True, ""


def _validate_git_readonly(rest: list[str]) -> tuple[bool, str]:
    """Allow read-only git subcommands; block mutating ones."""
    sub = _git_subcommand(rest)
    if not sub:
        return False, "git with no subcommand is not allowed before the evidence spec is validated"
    if sub in _BLOCKED_GIT_SUBCMDS:
        return False, f"git {sub} is not allowed before the evidence spec is validated (read-only git only)"
    if sub not in _ALLOWED_GIT_SUBCMDS:
        return False, f"git {sub} is not in the read-only git research whitelist"
    if sub == "branch":
        for tok in rest:
            if tok in ("-d", "-D", "-m", "-M", "--delete", "--move", "--rename"):
                return False, "git branch write/delete is not allowed before the evidence spec is validated"
    if sub == "tag":
        for tok in rest:
            if tok.startswith("-") and tok not in ("-l", "-n", "--list", "--contains", "--merged", "--no-merged"):
                if tok not in ("-v", "--verbose", "--sort", "--points-at", "--format"):
                    return False, "git tag create/delete is not allowed before the evidence spec is validated"
    if sub == "stash":
        for tok in rest:
            if tok in ("pop", "apply", "drop", "clear", "push", "save", "branch", "store"):
                return False, "git stash write is not allowed before the evidence spec is validated"
    if sub == "config":
        for tok in rest:
            if tok in ("set", "unset", "add", "remove-section", "rename-section", "--replace-all"):
                return False, "git config write is not allowed before the evidence spec is validated"
    if sub == "remote":
        for tok in rest:
            if tok in ("add", "remove", "rm", "rename", "set-url", "set-head", "set-branches", "update", "prune"):
                return False, "git remote write is not allowed before the evidence spec is validated"
    if sub == "push":
        for tok in rest:
            if tok in ("-f", "--force", "--force-if-includes"):
                return False, "git push --force is not allowed before the evidence spec is validated"
    if sub == "reflog":
        for tok in rest:
            if tok in ("expire", "delete"):
                return False, "git reflog write is not allowed before the evidence spec is validated"
    return True, ""


def _spec_cli_segment(rest: list[str]) -> tuple[bool, str]:
    """Classify a `python[3] ...` segment that may invoke the gate's spec CLI."""
    script = ""
    script_idx = -1
    for i, tok in enumerate(rest):
        if tok == "--" or tok.startswith("-"):
            continue
        script, script_idx = tok, i
        break
    if not script.replace("\\", "/").endswith("scripts/gate/spec.py"):
        return False, ""
    return _validate_spec_append_args(rest[script_idx + 1 :])


def _command_substitution_reason(text: str) -> str:
    """Reason if *text* contains a LIVE command/process substitution, else "".

    Single-quoted regions are literal in the shell, so `$(`, backticks and
    `<(`/`>(` inside single quotes are ignored (e.g. `rg '$(' file` is a real
    search, not substitution). Double-quoted `$(`/backtick still execute, so they
    are flagged. This is the only construct that can run an arbitrary command from
    an otherwise-whitelisted line, so it is rejected before the spec unlocks."""
    i, n = 0, len(text)
    in_single = in_double = False
    while i < n:
        ch = text[i]
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            i += 2  # backslash escapes the next char outside single quotes
            continue
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = not in_double
            i += 1
            continue
        if ch == "`":
            return "backtick command substitution is not allowed before the evidence spec is validated"
        if ch == "$" and i + 1 < n and text[i + 1] == "(":
            return "command substitution $(...) is not allowed before the evidence spec is validated"
        if not in_double and ch in "<>" and i + 1 < n and text[i + 1] == "(":
            return "process substitution <(...)/ >(...) is not allowed before the evidence spec is validated"
        i += 1
    return ""


def _declaration_segment(seg: str) -> tuple[bool, tuple[bool, str] | None]:
    """Classify a pure variable-declaration segment (`T=val`, `export A=1 B=2`).

    Returns (handled, result). When handled is True, result is the final
    (allowed, reason) for this segment. When False, the segment is not a pure
    declaration and normal command classification applies (so prefix-env on a real
    command, e.g. `FOO=bar rg ...`, still flows through _first_command). Command
    substitution is rejected upstream, so a value reaching here is a literal."""
    try:
        tokens = shlex.split(seg)
    except ValueError:
        return False, None
    if not tokens:
        return False, None
    reason = _agent_blocked_assignment_reason(tokens)
    if reason:
        return True, (False, reason)
    idx = 0
    if tokens[0] in _SAFE_DECL_PREFIXES:
        idx = 1
        if idx >= len(tokens):
            return True, (False, f"'{tokens[0]}' with no command is not a research command")
    names: list[str] = []
    for tok in tokens[idx:]:
        match = _ASSIGN_NAME_RE.match(tok)
        if not match:
            return False, None  # a non-assignment token -> not a pure declaration
        names.append(match.group(1))
    for name in names:
        if name in _DANGEROUS_ASSIGN_NAMES:
            return True, (False, f"{name}= changes command resolution and is not allowed before the evidence spec is validated")
    return True, (True, "")


def _allowed_segment(seg: str) -> tuple[bool, str]:
    handled, result = _declaration_segment(seg)
    if handled and result is not None:
        return result

    try:
        tokens = shlex.split(seg)
    except ValueError:
        tokens = seg.split()
    if not tokens:
        return False, "empty command"
    reason = _agent_blocked_assignment_reason(tokens)
    if reason:
        return False, reason

    command, rest = _first_command(tokens)
    if not command:
        return False, "no executable command found"

    base = _basename(command)
    if base in _ALLOWED_COMMANDS or base in READONLY_INSPECTION_COMMANDS:
        return True, ""
    if base in _SPEC_CLI_NAMES:
        ok, reason = _validate_spec_append_args(rest)
        if ok:
            return True, ""
        if reason:
            return False, reason
    if base in EXPLORE_SCRIPT_BASENAMES or _is_unifusion_script(command):
        return True, ""
    if base in _TRACE_INTERPRETERS:
        target_base = _basename(_trace_target_from_interpreter(rest))
        if target_base in EXPLORE_SCRIPT_BASENAMES or target_base in _UNIFUSION_SCRIPT_NAMES:
            return True, ""
    if base in _PY_INTERPRETERS:
        ok, reason = _spec_cli_segment(rest)
        if ok:
            return True, ""
        if reason:
            return False, reason
    if base in _AST_GREP_NAMES:
        return _validate_ast_grep_readonly(rest)
    if base == "git":
        return _validate_git_readonly(rest)
    return False, f"{base} is not in the Bash research whitelist"


def _segment_command_base(seg: str) -> str:
    """Return basename of the first executable command in *seg*, or ""."""
    try:
        tokens = shlex.split(seg)
    except ValueError:
        tokens = seg.split()
    if not tokens:
        return ""
    command, _rest = _first_command(tokens)
    if not command:
        return ""
    return _basename(command)


def _allowed_pipeline_sink(seg: str) -> tuple[bool, str]:
    try:
        tokens = shlex.split(seg)
    except ValueError:
        tokens = seg.split()
    if not tokens:
        return False, "empty pipeline segment"
    reason = _agent_blocked_assignment_reason(tokens)
    if reason:
        return False, reason
    command, _rest = _first_command(tokens)
    if not command:
        return False, "no executable command found in pipeline"
    base = _basename(command)
    if base in _PIPELINE_SINKS:
        return True, ""
    return False, f"{base} is not an allowed read-only pipeline sink"


def _allowed_pipeline_rest(seg: str) -> tuple[bool, str]:
    ok, reason = _allowed_pipeline_sink(seg)
    if ok:
        return True, ""
    return _allowed_segment(seg)


def _allowed_compound(compound: str) -> tuple[bool, str]:
    pipe_parts = _split_pipes(compound)
    if len(pipe_parts) == 1:
        return _allowed_segment(pipe_parts[0])
    ok, reason = _allowed_segment(pipe_parts[0])
    if not ok:
        return False, reason
    echo_source = _segment_command_base(pipe_parts[0]) == "echo"
    for seg in pipe_parts[1:]:
        if echo_source:
            ok, reason = _allowed_pipeline_sink(seg)
            if not ok:
                return (
                    False,
                    reason
                    or "echo may only pipe to read-only inspection commands (head, tail, wc, sort, uniq)",
                )
            continue
        ok, reason = _allowed_pipeline_rest(seg)
        if not ok:
            return False, reason
    return True, ""


def _explore_script_in_segment(seg: str) -> str | None:
    """Return trace.sh/websearch.sh when this shell segment invokes one."""
    try:
        tokens = shlex.split(seg)
    except ValueError:
        tokens = seg.split()
    if not tokens:
        return None
    command, rest = _first_command(tokens)
    if not command:
        return None
    base = _basename(command)
    if base in EXPLORE_SCRIPT_BASENAMES:
        return base
    if base in _TRACE_INTERPRETERS:
        target_base = _basename(_trace_target_from_interpreter(rest))
        if target_base in EXPLORE_SCRIPT_BASENAMES:
            return target_base
    return None


def explore_script_in_command(command: str) -> str | None:
    """Return trace.sh or websearch.sh when *command* invokes one, else None."""
    if not isinstance(command, str) or not command.strip():
        return None
    for line in _join_flag_lines(_logical_lines(command)):
        for compound in _split_compound(line):
            try:
                pipe_parts = _split_pipes(compound)
            except Exception:
                pipe_parts = [compound]
            for part in pipe_parts:
                found = _explore_script_in_segment(part)
                if found:
                    return found
    return None


def is_allowed_research_bash(command: str) -> tuple[bool, str]:
    """Return (allowed, reason). reason is non-empty when blocked."""
    if not isinstance(command, str) or not command.strip():
        return False, "empty command"

    for line in _join_flag_lines(_logical_lines(command)):
        subst = _command_substitution_reason(line)
        if subst:
            return False, subst
        for compound in _split_compound(line):
            allowed, reason = _allowed_compound(compound)
            if not allowed:
                return False, reason
    return True, ""
