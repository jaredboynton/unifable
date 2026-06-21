#!/usr/bin/env python3
"""Classify a Bash command as create/mutate (locked pre-evidence) or read/validation.

Denylist by design: default ALLOW. The evidence gate consults this only for `Bash`,
only when the effective grade is STANDARD+ and no valid spec exists yet (the research
phase). In that phase a command is BLOCKED when it clearly creates, deletes, moves,
or mutates files/state, installs packages, mutates git history, or performs a
network-mutating request. Everything else -- reads, searches, inspection, and
test/validation runners -- stays allowed, so the agent can always run the checks that
produce the acceptance evidence its spec needs. Once a valid spec exists, this is not
consulted at all (the action phase unlocks every tool).

A denylist (vs. an allowlist that blocks-by-default) is deliberate: it keeps
false-positive friction low (the failure mode the repo was previously burned by) while
still closing the "mutate off vibes before any evidence" hole. False negatives (a novel
mutating verb slipping through) are acceptable -- the deterministic WRITE_TOOLS gate
already covers the primary mutation vector (file edits).
"""

from __future__ import annotations

import re
import shlex

# Leading verb (basename) that is always a create/mutate action.
_MUTATING_CMDS = frozenset({
    "rm", "rmdir", "mv", "cp", "dd", "ln", "truncate", "shred",
    "mkdir", "touch", "tee", "install", "patch",
    "chmod", "chown", "chgrp", "rsync", "scp",
})

# command basename -> mutating subcommands (first non-flag arg). Read/validation
# subcommands (git log/diff/status/show, npm test, cargo build, go test ...) are
# intentionally absent so they stay allowed.
_MUTATING_SUB: dict[str, frozenset[str]] = {
    "git": frozenset({
        "add", "commit", "push", "pull", "reset", "checkout", "switch", "rm", "mv",
        "merge", "rebase", "cherry-pick", "stash", "tag", "clean", "restore",
        "apply", "am", "revert", "init",
    }),
    "npm": frozenset({"install", "i", "add", "remove", "rm", "uninstall", "ci", "publish", "update"}),
    "pnpm": frozenset({"install", "i", "add", "remove", "rm", "uninstall", "publish", "update"}),
    "yarn": frozenset({"add", "remove", "install", "up", "publish"}),
    "pip": frozenset({"install", "uninstall"}),
    "pip3": frozenset({"install", "uninstall"}),
    "brew": frozenset({"install", "uninstall", "upgrade", "reinstall", "remove", "rm"}),
    "apt": frozenset({"install", "remove", "purge", "upgrade"}),
    "apt-get": frozenset({"install", "remove", "purge", "upgrade"}),
    "cargo": frozenset({"install", "publish"}),
    "go": frozenset({"install"}),
    "docker": frozenset({"run", "rm", "rmi", "build", "push"}),
    "kubectl": frozenset({"apply", "delete", "create", "patch", "replace", "scale"}),
    "terraform": frozenset({"apply", "destroy", "import"}),
    "pulumi": frozenset({"up", "destroy"}),
}

# Wrapper verbs that prefix a real command (FOO=bar sudo nice <cmd> ...).
_WRAPPERS = frozenset({"sudo", "command", "env", "nice", "nohup", "time", "xargs", "stdbuf"})

# Operators that separate sub-commands (split coarsely; quoted operators may
# misclassify, which at worst over-blocks a rare read command -- acceptable).
_SPLIT_RE = re.compile(r"\|\||&&|\||;|\n")
# Env-assignment token: NAME=value.
_ENVVAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Redirection token: optional fd, one or two '>', optional inline target.
_REDIR_RE = re.compile(r"^\d*>>?(.*)$")


def _segments(command: str) -> list[str]:
    return [p.strip() for p in _SPLIT_RE.split(command) if p.strip()]


def _redirect_to_file(tokens: list[str]) -> bool:
    """True when a token redirects output to a real file (not /dev/null or an fd)."""
    for i, tok in enumerate(tokens):
        if tok.endswith("&1") or tok.endswith("&2"):
            continue  # 2>&1 and friends are fd dups, not file writes
        m = _REDIR_RE.match(tok)
        if not m:
            continue
        target = m.group(1).strip()
        if not target:  # bare '>' / '>>' -> target is the next token
            target = tokens[i + 1].strip() if i + 1 < len(tokens) else ""
        if target and target not in ("/dev/null", "/dev/stdout", "/dev/stderr") and not target.startswith("&"):
            return True
    return False


def _classify_segment(seg: str) -> str | None:
    try:
        tokens = shlex.split(seg)
    except ValueError:
        tokens = seg.split()
    if not tokens:
        return None

    # Skip leading env assignments and wrapper verbs to reach the real command.
    idx = 0
    while idx < len(tokens) and _ENVVAR_RE.match(tokens[idx]):
        idx += 1
    while idx < len(tokens) and tokens[idx].rsplit("/", 1)[-1] in _WRAPPERS:
        idx += 1
    if idx >= len(tokens):
        return None

    base = tokens[idx].rsplit("/", 1)[-1]
    rest = tokens[idx + 1:]

    if _redirect_to_file(tokens):
        return f"writes a file via redirect ({seg.strip()})"

    if base in _MUTATING_CMDS:
        return f"{base} (creates/mutates files)"

    if base in ("sed", "perl") and any(t == "-i" or t.startswith("-i") for t in rest):
        return f"{base} -i (in-place file edit)"

    if base == "find":
        if "-delete" in rest:
            return "find -delete"
        for j, tok in enumerate(rest):
            if tok in ("-exec", "-execdir") and j + 1 < len(rest):
                exo = rest[j + 1].rsplit("/", 1)[-1]
                if exo in _MUTATING_CMDS or exo in _MUTATING_SUB:
                    return f"find -exec {exo}"
        return None

    if base in ("curl", "wget"):
        for j, tok in enumerate(rest):
            if tok in ("-o", "-O", "--output", "-T", "--upload-file",
                       "-d", "--data", "--data-raw", "--data-binary", "-F", "--form") or tok.startswith("-o"):
                return f"{base} (writes a file / sends a body)"
            if tok in ("-X", "--request") and j + 1 < len(rest) and rest[j + 1].upper() in ("POST", "PUT", "DELETE", "PATCH"):
                return f"{base} {rest[j + 1].upper()} (network-mutating request)"
        return None  # plain GET is research -- allowed

    sub = _MUTATING_SUB.get(base)
    if sub:
        for tok in rest:
            if tok.startswith("-"):
                continue
            if tok in sub:
                return f"{base} {tok} (mutating)"
            break  # first positional is the subcommand; if not mutating, allow

    return None


def is_mutating_bash(command: str) -> tuple[bool, str]:
    """Return (is_mutating, reason). reason is empty when not mutating.

    Default ALLOW: only known create/mutate/install/network-mutating shapes block."""
    if not command or not isinstance(command, str):
        return (False, "")
    for seg in _segments(command):
        reason = _classify_segment(seg)
        if reason:
            return (True, reason)
    return (False, "")
