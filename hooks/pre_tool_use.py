#!/usr/bin/env python3
"""unifable pre-edit enforcement gate — PreToolUse.

Intercepts write tools (Edit / Write / MultiEdit / NotebookEdit / apply_patch),
Bash, and delegation tools (Task / Agent), and exits with code 2 (block) in
four cases:

  1. PROTECTED_PATHS: the target path resolves inside <cwd>/.unifable/ AND is
     not a spec file the model is allowed to write
     (.unifable/spec/<task_id>.json).  This prevents the model from modifying
     ledger state, goals, findings, or any other gate-internal artifact.

  2. EVIDENCE GATE — writes (unconditional): unless the effective grade is LIGHT,
     a valid spec carrying citation evidence (repo_context {cite, why},
     acceptance_criteria with live output, prior_art {cite, why} — all at STANDARD+) must
     exist for the current task before any edit is allowed. Authoring the spec
     file itself is always permitted (the no-brick escape).

  3. EVIDENCE GATE — Bash research whitelist (unconditional): in the research
     phase (grade STANDARD+, no valid spec yet), Bash may run only `ls`, `glob`,
     `rg`, or a file whose basename is `trace.sh`. A valid spec unlocks the action
     phase (all shell commands allowed). LIGHT waives. Classification:
     scripts/gate/bash_classify.py.

  4. EVIDENCE GATE — delegation lockdown (unconditional): in the research phase,
     Task/Agent are blocked until the same valid spec exists, so subagents cannot
     bypass the write/Bash gates. LIGHT waives.

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

from bash_classify import ALLOWED_RESEARCH_BASH, is_allowed_research_bash
from ledger import emit_json, load_ledger, read_stdin_json
from spec import GRADES, contract_string, load_spec, resolve_session_id, spec_path, validate_spec

# ---------------------------------------------------------------------------
# Tool names across both hosts (Claude Code and Codex)
# ---------------------------------------------------------------------------

WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})
DELEGATION_TOOLS = frozenset({"Task", "Agent"})

# ---------------------------------------------------------------------------
# Protected path patterns inside .unifable/
# The model MAY write .unifable/spec/<task_id>.json; everything else is off-limits.
# ---------------------------------------------------------------------------

_GATE_PREFIXES = ("ledger", "goals.json", "findings.json", "state")


def _unifable_dir(cwd: str | Path) -> Path:
    return Path(cwd).resolve() / ".unifable"


def _is_protected(target: str | Path, cwd: str | Path) -> bool:
    """Return True when *target* is ANY path under <cwd>/.unifable/.

    Specs are CLI-only: the model mutates them via spec.py (create / add-task /
    deliver / validate-task), never with Edit/Write. Hand-editing the spec JSON is
    blocked so an agent cannot delete tasks or fake a validated status. ledger,
    goals, findings, and state were already protected; spec/ now joins them.
    """
    try:
        resolved = Path(target).resolve()
        resolved.relative_to(_unifable_dir(cwd))
    except (ValueError, OSError):
        return False  # not under .unifable/ — not protected
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
    """Derive the active spec key. Prefer the ledger's `active_task` (the prompt
    hash gate_prompt.py pinned, locked-until-complete) so the gate looks at the
    spec for the task in flight. Fall back to stdin session_id, then host env
    (CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID), then 'default'."""
    try:
        active = load_ledger(input_data).get("active_task")
        if active:
            return str(active)
    except Exception:
        pass
    return resolve_session_id(input_data, default="default") or "default"


# ---------------------------------------------------------------------------
# Block helper
# ---------------------------------------------------------------------------

def _block(reason: str) -> int:
    print(f"unifable pre-edit gate: {reason}", file=sys.stderr)
    return 2


def _citation_reasons(spec: dict, input_data: dict, cwd: str, require_commands: bool) -> list[str]:
    """Reasons the spec's citations are not backed by real session tool activity.
    Empty when the cross-check is disabled or anything fails (fail open)."""
    try:
        from citations import activity_from_ledger, enabled, verify_citations

        if not enabled():
            return []
        activity = activity_from_ledger(load_ledger(input_data))
        return verify_citations(spec, activity, cwd, require_commands=require_commands)
    except Exception:
        return []


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
    carrying citation evidence (repo_context {cite, why}, acceptance_criteria with
    live output, prior_art {cite, why}) must exist for any STANDARD+ task. LIGHT waives."""
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
            "Specs are CLI-only -- create one with: python3 scripts/gate/spec.py "
            f"create --task-id {task_id} --goal '<restated goal>' "
            "--task 'title::<runnable check>' --repo-context 'path:line::why' --prior-art '<url>::why'. "
            f"{contract_string(grade, True)}"
        )

    ok, reasons = validate_spec(spec, grade, require_evidence=True)
    if not ok:
        sp = spec_path(cwd, task_id)
        detail = "; ".join(reasons)
        return _block(
            f"spec at {sp} does not satisfy grade {grade}: {detail}. "
            "Fix the spec before proceeding with edits."
        )

    cited = _citation_reasons(spec, input_data, cwd, require_commands=False)
    if cited:
        return _block(
            "spec citations are not backed by real activity this session: "
            + "; ".join(cited)
        )

    emit_json({})
    return 0


def _enforce_bash(input_data: dict, tool_input: dict, cwd: str) -> int:
    """Research-phase whitelist for Bash (unconditional, no env disable).

    Research phase (no valid spec): allow only ls, glob, rg, and trace.sh so the
    agent can inspect the tree and run the explore skill. Action phase (valid
    spec): all shell commands are allowed. LIGHT waives entirely."""
    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        emit_json({})
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    if spec is not None:
        ok, _ = validate_spec(spec, grade, require_evidence=True)
        if ok and not _citation_reasons(spec, input_data, cwd, require_commands=False):
            emit_json({})  # action phase unlocked
            return 0

    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    allowed, why = is_allowed_research_bash(command)
    if not allowed:
        sp = spec_path(cwd, task_id)
        return _block(
            f"Bash command blocked before evidence spec validation: {why}. "
            f"Allowed before unlock: {ALLOWED_RESEARCH_BASH}. "
            f"To unblock other Bash commands, create a valid task spec at {sp} through the trusted "
            "spec workflow, with repo_context {cite,why}, acceptance_criteria with live output, "
            "and prior_art {cite,why}; once that spec validates, retry the command."
        )

    emit_json({})
    return 0


def _enforce_delegation(input_data: dict, tool_name: str, cwd: str) -> int:
    """Block Task/Agent until a valid evidence spec unlocks the action phase."""
    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        emit_json({})
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    if spec is not None:
        ok, _ = validate_spec(spec, grade, require_evidence=True)
        if ok and not _citation_reasons(spec, input_data, cwd, require_commands=False):
            emit_json({})
            return 0

    sp = spec_path(cwd, task_id)
    return _block(
        f"{tool_name} is blocked before evidence spec validation so delegated work cannot bypass "
        "the write/Bash gates. Still available before unlock: Read/Grep/Glob/web/source-fetch tools "
        f"and Bash commands limited to {ALLOWED_RESEARCH_BASH}. To unblock Task/Agent and broader "
        f"Bash, create a valid task spec at {sp} through the trusted spec workflow, with repo_context "
        "{cite,why}, acceptance_criteria with live output, and prior_art {cite,why}; once that spec "
        "validates, retry."
    )


def _enforce_breaker(input_data: dict) -> int | None:
    """Overconfidence/groundedness breaker. Runs the debounced gpt-realtime-2 judge
    (<=1 call / 15s per session+prompt key) over the recent transcript. Returns a
    _block() exit code carrying the steering prompt when a mutation tool
    (Write/Edit/Bash) is blocked because the model asserted something confidently
    without backing it up; returns None otherwise (reads/web are never blocked).
    Fails open (returns None) on any error."""
    try:
        import time

        from groundedness import evaluate as breaker_evaluate
        from ledger import save_ledger

        ledger = load_ledger(input_data)
        active = str(ledger.get("active_task") or "")
        block, steering = breaker_evaluate(input_data, ledger, time.time(), active)
        save_ledger(input_data, ledger)
        if block:
            return _block(steering or (
                "Groundedness breaker: you asserted something confidently without "
                "backing it up. Your tools are restricted to read-only ones (Read, "
                "WebSearch, WebFetch, Grep, Glob) until you ground the claim."
            ))
    except Exception:
        return None  # fail open on any breaker/judge failure
    return None


def main() -> int:
    input_data = read_stdin_json()

    tool_name = str(input_data.get("tool_name") or "")
    tool_input = input_data.get("tool_input") or {}
    cwd = str(input_data.get("cwd") or os.getcwd())

    # --- Overconfidence/groundedness breaker (runs on EVERY tool; judge debounced
    #     to <=1 call / 15s per session+prompt). Blocks ONLY mutation tools when
    #     gpt-realtime-2 flags a confident unproven claim; reads/web stay free. ---
    breaker_block = _enforce_breaker(input_data)
    if breaker_block is not None:
        return breaker_block

    # --- Write tools: protected paths + evidence gate (unconditional) ---
    if tool_name in WRITE_TOOLS:
        target = _target_path(tool_name, tool_input)

        # Guard 1: PROTECTED_PATHS (includes .unifable/spec/* — specs are CLI-only)
        if target and _is_protected(target, cwd):
            return _block(
                f"write to protected unifable state file '{target}' is not allowed. "
                "Specs are CLI-only: create and mutate them via "
                "`python3 scripts/gate/spec.py` (create / add-task / deliver / "
                "validate-task), never by hand-editing the JSON. ledger, goals, "
                "findings, and state are off-limits too."
            )

        return _enforce_spec(input_data, cwd)

    # --- Bash: research whitelist (unconditional) ---
    if tool_name == "Bash":
        return _enforce_bash(input_data, tool_input, cwd)

    # --- Delegation: locked until the same evidence spec unlocks action phase ---
    if tool_name in DELEGATION_TOOLS:
        return _enforce_delegation(input_data, tool_name, cwd)

    # Any other tool — nothing to gate (read/search/web stay free).
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
