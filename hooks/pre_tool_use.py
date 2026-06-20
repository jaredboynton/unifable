#!/usr/bin/env python3
"""unifable pre-edit enforcement gate — PreToolUse.

Intercepts write tools (Edit / Write / MultiEdit / NotebookEdit / apply_patch)
and exits with code 2 (block) in two cases:

  1. PROTECTED_PATHS: the target path resolves inside <cwd>/.unifable/ AND is
     not a spec file the model is allowed to write
     (.unifable/spec/<task_id>.json).  This prevents the model from modifying
     ledger state, goals, findings, or any other gate-internal artifact.

  2. SPEC GATE: when UNIFABLE_SPEC_GATE=1 and the effective grade is not LIGHT,
     a valid spec must exist for the current task before any edit is allowed.

Opt-in via UNIFABLE_SPEC_GATE=1.  Default is OFF (emit {} exit 0) so
existing sessions are unaffected.

Grade is read from UNIFABLE_GRADE (LIGHT / STANDARD / HEAVY); defaults to
STANDARD.  Map quick->LIGHT, normal->STANDARD, deep->HEAVY in classify_task.py.

Fails open on any exception: emits {} and exits 0 so the host is never
interrupted by gate errors.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "gate"))

from ledger import emit_json, read_stdin_json
from spec import GRADES, load_spec, spec_path, validate_spec

# ---------------------------------------------------------------------------
# Write-tool names across both hosts (Claude Code and Codex)
# ---------------------------------------------------------------------------

WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})

# ---------------------------------------------------------------------------
# Protected path patterns inside .unifable/
# The model MAY write .unifable/spec/<task_id>.json; everything else is off-limits.
# ---------------------------------------------------------------------------

_GATE_PREFIXES = ("ledger", "goals.json", "findings.json", "state")


def _unifable_dir(cwd: str | Path) -> Path:
    return Path(cwd).resolve() / ".unifable"


def _is_protected(target: str | Path, cwd: str | Path) -> bool:
    """Return True when *target* is a unifable state file the model must not touch.

    Permitted: .unifable/spec/<anything>  (the model authors spec files)
    Blocked:   .unifable/ledger*
               .unifable/goals.json
               .unifable/findings.json
               .unifable/state/
               .unifable/<anything else not under spec/>
    """
    try:
        resolved = Path(target).resolve()
        unifable = _unifable_dir(cwd)
        # Must be under .unifable/ to be protected at all.
        resolved.relative_to(unifable)
    except (ValueError, OSError):
        return False  # not under .unifable/ — not protected

    # .unifable/spec/* is allowed
    spec_dir = unifable / "spec"
    try:
        resolved.relative_to(spec_dir)
        return False  # model may write spec files
    except ValueError:
        pass

    # Everything else under .unifable/ is protected
    return True


# ---------------------------------------------------------------------------
# Extract the target file path from tool input
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Task ID derivation
# ---------------------------------------------------------------------------

def _task_id(input_data: dict) -> str:
    """Derive a stable task ID from session_id (same as ledger_key prefix)."""
    return str(input_data.get("session_id") or "default")


# ---------------------------------------------------------------------------
# Block helper
# ---------------------------------------------------------------------------

def _block(reason: str) -> int:
    print(f"unifable pre-edit gate: {reason}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Main gate logic
# ---------------------------------------------------------------------------

def _effective_grade() -> str:
    grade = os.environ.get("UNIFABLE_GRADE", "STANDARD").upper().strip()
    return grade if grade in GRADES else "STANDARD"


def _spec_gate_active() -> bool:
    return os.environ.get("UNIFABLE_SPEC_GATE", "0").strip() == "1"


def main() -> int:
    input_data = read_stdin_json()

    tool_name = str(input_data.get("tool_name") or "")
    tool_input = input_data.get("tool_input") or {}
    cwd = str(input_data.get("cwd") or os.getcwd())

    # Not a write tool — nothing to gate.
    if tool_name not in WRITE_TOOLS:
        emit_json({})
        return 0

    target = _target_path(tool_name, tool_input)

    # --- Guard 1: PROTECTED_PATHS ---
    if target and _is_protected(target, cwd):
        return _block(
            f"write to protected unifable state file '{target}' is not allowed. "
            "The model must not modify ledger, goals, findings, or state artifacts directly."
        )

    # --- Guard 2: Spec gate (opt-in) ---
    if not _spec_gate_active():
        emit_json({})
        return 0

    grade = _effective_grade()

    # LIGHT grade waives the spec requirement entirely.
    if grade == "LIGHT":
        emit_json({})
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)

    if spec is None:
        sp = spec_path(cwd, task_id)
        return _block(
            f"no spec artifact found for task '{task_id}' (grade={grade}). "
            f"Write {sp} with at minimum 'restated_goal' and one 'acceptance_criteria' entry "
            "before editing implementation files. "
            "See: python3 scripts/gate/spec.py init --task-id <task-id>."
        )

    ok, reasons = validate_spec(spec, grade)
    if not ok:
        sp = spec_path(cwd, task_id)
        detail = "; ".join(reasons)
        return _block(
            f"spec at {sp} does not satisfy grade {grade}: {detail}. "
            "Fix the spec before proceeding with edits."
        )

    emit_json({})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — fail open
        emit_json({})
        print(f"unifable pre-tool hook failed open: {exc}", file=sys.stderr)
        raise SystemExit(0)
