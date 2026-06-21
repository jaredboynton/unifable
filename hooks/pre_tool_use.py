#!/usr/bin/env python3
"""unifable pre-edit enforcement gate — PreToolUse.

Intercepts write tools (Edit / Write / MultiEdit / NotebookEdit / apply_patch)
and Bash, and exits with code 2 (block) in three cases:

  1. PROTECTED_PATHS: the target path resolves inside <cwd>/.unifable/ AND is
     not a spec file the model is allowed to write
     (.unifable/spec/<task_id>.json).  This prevents the model from modifying
     ledger state, goals, findings, or any other gate-internal artifact.

  2. EVIDENCE GATE — writes (unconditional): unless the effective grade is LIGHT,
     a valid spec carrying citation evidence (must_read {cite, why},
     acceptance_criteria with live output, prior_art URL — all at STANDARD+) must
     exist for the current task before any edit is allowed. Authoring the spec
     file itself is always permitted (the no-brick escape).

  3. EVIDENCE GATE — Bash create/mutate lockdown (unconditional): in the research
     phase (grade STANDARD+, no valid spec yet) a Bash command that creates,
     deletes, moves, or mutates files/state, installs packages, mutates git
     history, or performs a network-mutating request is blocked. Read, search,
     web, and test/validation runners stay available so the agent can produce the
     evidence its spec needs. A valid spec unlocks the action phase (all shell
     commands allowed). LIGHT waives. Classification: scripts/gate/bash_classify.py.

The evidence gate is always on — there is no env disable. LIGHT (quick) tasks are
waived by grade, authoring the spec is always allowed (no-brick), and the hook
fails open on any exception so a gate bug never interrupts the host.

Grade is read from UNIFABLE_GRADE, else the session ledger, else STANDARD
(LIGHT / STANDARD / HEAVY); quick->LIGHT, normal->STANDARD, deep->HEAVY.

Fails open on any exception: emits {} and exits 0 so the host is never
interrupted by gate errors.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "gate"))

from bash_classify import is_mutating_bash
from ledger import emit_json, load_ledger, read_stdin_json
from spec import GRADES, contract_string, load_spec, spec_path, validate_spec

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


def _is_spec_path(target: str | Path, cwd: str | Path) -> bool:
    """Return True when *target* is the model's evidence spec under .unifable/spec/.

    The model must always be able to author or update its spec, even before one
    exists — otherwise the gate bricks (writing the spec would itself require a
    spec). This is the no-brick escape that lets the agent satisfy the gate.
    """
    try:
        resolved = Path(target).resolve()
        resolved.relative_to(_unifable_dir(cwd) / "spec")
        return True
    except (ValueError, OSError):
        return False


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

def _effective_grade(input_data: dict | None = None) -> str:
    """Grade from UNIFABLE_GRADE, else this session's ledger, else STANDARD.

    Reading the ledger (written by gate_prompt.py at UserPromptSubmit) lets the
    default-on gate respect the task classification: a quick task graded LIGHT is
    waived, so trivial edits are not over-gated."""
    grade = os.environ.get("UNIFABLE_GRADE", "").upper().strip()
    if grade not in GRADES and input_data is not None:
        try:
            grade = (load_ledger(input_data).get("grade") or "").upper().strip()
        except Exception:
            grade = ""
    return grade if grade in GRADES else "STANDARD"


def _enforce_spec(input_data: dict, cwd: str) -> int:
    """Block a write tool unless a valid evidence spec exists for the task.

    The evidence gate is unconditional — there is no env disable. A valid spec
    carrying citation evidence (must_read {cite, why}, acceptance_criteria with
    live output, prior_art URL) must exist for any STANDARD+ task. LIGHT waives."""
    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        emit_json({})
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    if spec is None:
        sp = spec_path(cwd, task_id)
        return _block(
            f"no spec artifact found for task '{task_id}' (grade={grade}). "
            f"Write {sp} before editing implementation files. "
            f"{contract_string(grade, True)} "
            "Init: python3 scripts/gate/spec.py init --task-id <task-id>."
        )

    ok, reasons = validate_spec(spec, grade, require_evidence=True)
    if not ok:
        sp = spec_path(cwd, task_id)
        detail = "; ".join(reasons)
        return _block(
            f"spec at {sp} does not satisfy grade {grade}: {detail}. "
            "Fix the spec before proceeding with edits."
        )

    emit_json({})
    return 0


def _enforce_bash(input_data: dict, tool_input: dict, cwd: str) -> int:
    """Create/mutate lockdown for Bash (unconditional, no env disable).

    Research phase (no valid spec): block only create/mutate commands; read,
    search, web, and test/validation runners stay available so the agent can
    gather the evidence its spec needs. Action phase (valid spec): all shell
    commands are allowed. LIGHT waives entirely."""
    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        emit_json({})
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    if spec is not None:
        ok, _ = validate_spec(spec, grade, require_evidence=True)
        if ok:
            emit_json({})  # action phase unlocked
            return 0

    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    mutating, why = is_mutating_bash(command)
    if mutating:
        sp = spec_path(cwd, task_id)
        return _block(
            f"'{why}' is a create/mutate action — locked until {sp} documents your evidence "
            "(must_read {cite,why}, acceptance_criteria with live output, prior_art URL). "
            "Read/grep/web and test/validation runners stay available: gather evidence, "
            "write the spec, then retry."
        )

    emit_json({})
    return 0


def main() -> int:
    input_data = read_stdin_json()

    tool_name = str(input_data.get("tool_name") or "")
    tool_input = input_data.get("tool_input") or {}
    cwd = str(input_data.get("cwd") or os.getcwd())

    # --- Write tools: protected paths + evidence gate (unconditional) ---
    if tool_name in WRITE_TOOLS:
        target = _target_path(tool_name, tool_input)

        # Guard 1: PROTECTED_PATHS
        if target and _is_protected(target, cwd):
            return _block(
                f"write to protected unifable state file '{target}' is not allowed. "
                "The model must not modify ledger, goals, findings, or state artifacts directly."
            )

        # No-brick escape: authoring/updating the evidence spec itself is always
        # allowed, even before one exists — otherwise writing the spec would
        # require a spec. The only write the gate lets through unconditionally.
        if target and _is_spec_path(target, cwd):
            emit_json({})
            return 0

        return _enforce_spec(input_data, cwd)

    # --- Bash: create/mutate lockdown (unconditional) ---
    if tool_name == "Bash":
        return _enforce_bash(input_data, tool_input, cwd)

    # Any other tool — nothing to gate (read/search/web/subagents stay free).
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
