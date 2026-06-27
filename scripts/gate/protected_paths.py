#!/usr/bin/env python3
"""Protected-path guards for the unifable pre-tool gate.

The repo-local <cwd>/.unifable/ tree and the global keyed spec store
(<data_root>/specs/) are CLI-only: the model mutates specs via the `unifable`
CLI, never with Edit/Write/apply_patch or a shell redirect/sed/rm/tee. This
module resolves the file path(s) a write tool or Bash command would mutate and
reports whether any of them land inside a protected root. Fails open: any error
returns "not protected" so a guard bug can never hard-block a session.

Extracted from hooks/pre_tool_use.py; host-agnostic (no hooks/ imports).
"""
from __future__ import annotations

import re
import shlex
from pathlib import Path

try:
    from ledger import data_root
except ImportError:  # pragma: no cover
    from scripts.gate.ledger import data_root


_GATE_PREFIXES = ("ledger", "findings.json", "state")


def _unifable_dir(cwd: str | Path) -> Path:
    return Path(cwd).resolve() / ".unifable"


def _is_protected(target: str | Path, cwd: str | Path) -> bool:
    """Return True when *target* is under the repo-local <cwd>/.unifable/ OR under
    the global keyed spec store (<data_root>/specs/).

    Specs are CLI-only: the model mutates them via unifable (restate / add-task /
    set-primary / add-frontier), never with Edit/Write. Hand-editing
    the spec JSON is blocked so an agent cannot delete tasks or fake a validated
    status. The spec now lives globally under <data_root>/specs/<dir>/<session>/,
    so that root is protected too; the repo-local .unifable/ (findings, residual
    state) stays protected as before.
    """
    try:
        resolved = Path(target).expanduser().resolve()
    except (ValueError, OSError):
        return False
    for root in (_unifable_dir(cwd), data_root() / "specs"):
        try:
            resolved.relative_to(root)
            return True
        except (ValueError, OSError):
            continue
    return False


def _target_path(tool_name: str, tool_input: dict) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    # Claude Code: Edit / Write / NotebookEdit carry file_path
    fp = tool_input.get("file_path")
    if fp:
        return str(fp)
    # MultiEdit carries edits[0].file_path or a top-level path
    edits = tool_input.get("edits")
    if isinstance(edits, list) and edits:
        fp = edits[0].get("file_path") if isinstance(edits[0], dict) else None
        if fp:
            return str(fp)
    # apply_patch: path is embedded in the patch text — use cwd as a fallback
    # so the PROTECTED_PATHS guard still fires for in-.unifable patches.
    # (The spec gate uses cwd-level reasoning anyway.)
    patch = tool_input.get("patch") or tool_input.get("content") or ""
    if isinstance(patch, str) and ".unifable" in patch:
        # Return a sentinel that will trigger the protected-path check.
        return ".unifable/_patch"
    return None


_APPLY_PATCH_PATH_RE = re.compile(
    r"^\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(.+?)\s*$"
    r"|^\*\*\*\s+Move\s+(?:to|from):\s*(.+?)\s*$"
    r"|^(?:---|\+\+\+)\s+(?:[ab]/)?(.+?)\s*$",
    re.MULTILINE,
)


def _apply_patch_targets(tool_input) -> list[str]:
    """Best-effort extraction of every file path an apply_patch envelope touches.

    Host-shape-robust: gathers candidate patch text from EVERY string value in
    tool_input (and tool_input itself when it is a str), then matches both the
    Codex "*** Update/Add/Delete/Move ... File:" header form and the git-style
    "--- a/" / "+++ b/" form. Fails open: returns [] on any error."""
    try:
        chunks: list[str] = []
        if isinstance(tool_input, str):
            chunks.append(tool_input)
        elif isinstance(tool_input, dict):
            for v in tool_input.values():
                if isinstance(v, str):
                    chunks.append(v)
        text = "\n".join(chunks)
        if not text:
            return []
        targets: list[str] = []
        for m in _APPLY_PATCH_PATH_RE.finditer(text):
            path = m.group(1) or m.group(2) or m.group(3)
            if not path:
                continue
            path = path.strip()
            if not path or path == "/dev/null":
                continue
            targets.append(path)
        return targets
    except Exception:
        return []


def _write_targets(tool_name: str, tool_input) -> list[str]:
    """All file paths a write tool would mutate. apply_patch can touch several
    files in one envelope; the Claude write tools touch exactly one (or none)."""
    try:
        if tool_name == "apply_patch":
            return _apply_patch_targets(tool_input)
        t = _target_path(tool_name, tool_input)
        return [t] if t is not None else []
    except Exception:
        return []


_BASH_EXTRA_MUTATE_RE = re.compile(
    r"(?i)(?:>>?|"  # output redirect
    r"\btee\b|"  # tee writes its file args
    r"\bsed\b[^|;&]*\s-[A-Za-z]*i|"  # sed -i / -Ei in-place
    r"\bperl\b[^|;&]*\s-[A-Za-z]*i)"  # perl -i in-place
)


def _bash_protected_write(command: str, cwd: str | Path) -> str | None:
    """Return a protected path a Bash *command* would mutate, else None.

    Specs and ledger live under protected roots and are CLI-only, but the action
    phase allows all shell — so `echo x > spec.json`, `sed -i ... spec.json`,
    `rm spec.json`, `tee spec.json`, or an apply_patch heredoc would otherwise
    slip past the write-tool guard entirely. This runs unconditionally on Bash.

    Conservative by design: only fires when the command looks mutating
    (MUTATING_BASH_RE — apply_patch|chmod|mkdir|mv|cp|rm|touch|redirects|...) and
    then any token (or embedded apply_patch path) that resolves into a protected
    root blocks it. A mutating command that merely references a protected path is
    blocked; that is acceptable and safe. Fails open: returns None on any error."""
    try:
        if not isinstance(command, str) or not command:
            return None
        try:
            from parse_tool_result import MUTATING_BASH_RE
        except Exception:
            return None
        # MUTATING_BASH_RE covers apply_patch|chmod|mkdir|mv|cp|rm|touch|builds but
        # NOT shell write-redirects (`>`, `>>`), in-place editors (`sed -i`,
        # `perl -i`), or `tee` — the very ways `echo x > spec.json` / `sed -i ...
        # spec.json` mutate gate state. Treat those as mutating too so the guard
        # fires before the action phase opens all shell.
        is_mutating = bool(MUTATING_BASH_RE.search(command)) or bool(_BASH_EXTRA_MUTATE_RE.search(command))
        if not is_mutating:
            return None
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = re.split(r"[\s;|&<>()\"']+", command)
        # apply_patch heredocs embed their target paths in the command body.
        candidates = list(tokens) + _apply_patch_targets({"command": command})
        for token in candidates:
            if not token:
                continue
            if _is_protected(token, cwd):
                return token
        return None
    except Exception:
        return None


# Public API (stable names for callers and unit tests). The underscore-prefixed
# functions above are the implementation; these are the surface other modules use.
is_protected = _is_protected
write_targets = _write_targets
bash_protected_write = _bash_protected_write


def is_protected_write(tool_name: str, tool_input, cwd: str | Path) -> tuple[bool, str | None]:
    """Whether a write tool's target(s) land in a protected root.

    Returns (blocked, first_protected_path). apply_patch can touch several files
    in one envelope, so every target is checked. Fails open: any error yields
    (False, None)."""
    try:
        for target in _write_targets(tool_name, tool_input):
            if _is_protected(target, cwd):
                return True, target
        return False, None
    except Exception:
        return False, None
