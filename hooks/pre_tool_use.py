#!/usr/bin/env python3
"""unifable pre-edit enforcement gate — PreToolUse.

Intercepts write tools (Edit / Write / MultiEdit / NotebookEdit / apply_patch),
Bash, and delegation tools (Task / Agent), and exits with code 2 (block) in
four cases:

  1. PROTECTED_PATHS: the target path resolves inside <cwd>/.unifable/ or under
     the global keyed spec store (<data_root>/specs/). Specs are CLI-only, so this
     prevents the model from modifying the spec, ledger state, goals, findings, or
     any other gate-internal artifact with Edit/Write.

  2. EVIDENCE GATE — writes (unconditional): unless the effective grade is LIGHT,
     a valid spec carrying citation evidence (repo_context {cite, why},
     acceptance_criteria with live output, prior_art {cite, why} — all at STANDARD+) must
     exist for the current task before any edit is allowed. The spec is auto-created
     by the prompt hook and driven via the spec.py CLI (the no-brick escape), never
     hand-written.

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
from evidence_policy import resolve_grade
from ledger import data_root, emit_json, load_ledger, read_stdin_json
from spec import canonical_project_root, contract_string, format_spec_location, load_spec, resolve_session_id, spec_path, validate_spec

# ---------------------------------------------------------------------------
# Tool names across both hosts (Claude Code and Codex)
# ---------------------------------------------------------------------------

WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"})
DELEGATION_TOOLS = frozenset({"Task", "Agent"})

# ---------------------------------------------------------------------------
# Protected paths: the repo-local <cwd>/.unifable/ AND the global keyed spec store
# (<data_root>/specs/). Specs are CLI-only (spec.py) -- never model-writable.
# ---------------------------------------------------------------------------

_GATE_PREFIXES = ("ledger", "goals.json", "findings.json", "state")


def _unifable_dir(cwd: str | Path) -> Path:
    return Path(cwd).resolve() / ".unifable"


def _is_protected(target: str | Path, cwd: str | Path) -> bool:
    """Return True when *target* is under the repo-local <cwd>/.unifable/ OR under
    the global keyed spec store (<data_root>/specs/).

    Specs are CLI-only: the model mutates them via unifable (restate / add-task / dispute),
    never with Edit/Write. Hand-editing
    the spec JSON is blocked so an agent cannot delete tasks or fake a validated
    status. The spec now lives globally under <data_root>/specs/<dir>/<session>/,
    so that root is protected too; the repo-local .unifable/ (findings, residual
    state) stays protected as before.
    """
    try:
        resolved = Path(target).resolve()
    except (ValueError, OSError):
        return False
    for root in (_unifable_dir(cwd), data_root() / "specs"):
        try:
            resolved.relative_to(root)
            return True
        except (ValueError, OSError):
            continue
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
    """Derive the spec key. The evidence spec is one per (directory, session), so
    the key is the resolved session id -- stdin session_id, then host env
    (CLAUDE_CODE_SESSION_ID / CODEX_THREAD_ID), then 'default'. (The ledger's
    `active_task` is now the per-prompt hash for the breaker, not the spec key.)"""
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

    Resolution and precedence live in evidence_policy.resolve_grade (the single
    policy boundary): valid UNIFABLE_GRADE > active task's task_mode -> derived
    grade > legacy ledger grade > STANDARD. Reading the ledger (written by
    gate_prompt.py at UserPromptSubmit) lets the default-on gate respect the task
    classification: a quick task graded LIGHT is waived, so trivial edits are not
    over-gated."""
    ledger: dict = {}
    if input_data is not None:
        try:
            ledger = load_ledger(input_data)
        except Exception:
            ledger = {}
    return resolve_grade(ledger, os.environ.get("UNIFABLE_GRADE"))


def _enforce_spec(input_data: dict, cwd: str) -> int:
    """Block a write tool unless a valid evidence spec exists for the task.

    The evidence gate is unconditional — there is no env disable. A valid spec
    carrying citation evidence (repo_context {cite, why}, acceptance_criteria with
    live output, prior_art {cite, why}) must exist for any STANDARD+ task. LIGHT waives."""
    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    if spec is not None:
        try:
            from citations import activity_from_ledger, sync_citations_from_activity
            from spec import save_spec

            if sync_citations_from_activity(spec, activity_from_ledger(load_ledger(input_data)), cwd):
                save_spec(cwd, task_id, spec)
        except Exception:
            pass
    if spec is None:
        loc = format_spec_location(cwd, task_id)
        return _block(
            f"no evidence spec for session '{task_id}' (grade={grade}). The spec is "
            "auto-created on the hook path; build it through the append-only CLI "
            "(never edit the JSON, never run create):\n"
            f"{loc}\n"
            f"  unifable restate '<your restatement>'\n"
            f"  unifable add-task --title '<requirement>' --check '<runnable check>'\n"
            f"Citations sync from reads/fetches automatically. "
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

    return 0


def _enforce_bash(input_data: dict, tool_input: dict, cwd: str) -> int:
    """Research-phase whitelist for Bash (unconditional, no env disable).

    Research phase (no valid spec): allow only ls, glob, rg, and trace.sh so the
    agent can inspect the tree and run the explore skill. Action phase (valid
    spec): all shell commands are allowed. LIGHT waives entirely."""
    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    if spec is not None:
        ok, _ = validate_spec(spec, grade, require_evidence=True)
        if ok and not _citation_reasons(spec, input_data, cwd, require_commands=False):
            return 0  # action phase unlocked

    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    allowed, why = is_allowed_research_bash(command)
    if not allowed:
        loc = format_spec_location(cwd, task_id)
        return _block(
            f"Bash command blocked before evidence spec validation: {why}. "
            f"Allowed before unlock: {ALLOWED_RESEARCH_BASH}. "
            f"To unblock other Bash, restate the goal and add requirements with "
            f"`unifable restate` / `unifable add-task` "
            f"(citations sync from activity automatically):\n{loc}"
        )

    return 0


def _enforce_delegation(input_data: dict, tool_name: str, cwd: str) -> int:
    """Block Task/Agent until a valid evidence spec unlocks the action phase."""
    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    if spec is not None:
        ok, _ = validate_spec(spec, grade, require_evidence=True)
        if ok and not _citation_reasons(spec, input_data, cwd, require_commands=False):
            return 0

    loc = format_spec_location(cwd, task_id)
    return _block(
        f"{tool_name} is blocked before evidence spec validation so delegated work cannot bypass "
        "the write/Bash gates. Still available before unlock: Read/Grep/Glob/web/source-fetch tools "
        f"and Bash commands limited to {ALLOWED_RESEARCH_BASH}. To unblock Task/Agent and broader "
        f"Bash, restate the goal and add requirements with `unifable restate` / `unifable add-task` "
        f"(citations sync from activity automatically):\n"
        f"{loc}"
    )


def _emit_allow(notify: str = "") -> int:
    if notify and notify.strip():
        emit_json(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": notify.strip(),
                }
            }
        )
    else:
        emit_json({})
    return 0


def _enforce_breaker(input_data: dict) -> tuple[int | None, str]:
    """Overconfidence/groundedness breaker. Returns (block_exit_code, lift_notify)."""
    try:
        import time

        from breaker_state import load_breaker, save_breaker
        from groundedness import evaluate_pre_tool

        ledger = load_ledger(input_data)
        active = str(ledger.get("active_task") or "")
        breaker = load_breaker(input_data)
        block, steering, notify = evaluate_pre_tool(input_data, breaker, time.time(), active)
        save_breaker(input_data, breaker)
        if block:
            events = breaker.get("events") if isinstance(breaker.get("events"), list) else []
            if events and events[-1].get("kind") == "REINSTATE":
                steering = f"Groundedness breaker reinstated: {steering}"
            return _block(steering or (
                "Groundedness breaker: you asserted something confidently without "
                "backing it up. Your tools are restricted to read-only ones (Read, "
                "WebSearch, WebFetch, Grep, Glob) and whitelisted research Bash "
                f"({ALLOWED_RESEARCH_BASH}) until you ground the claim."
            )), ""
        return None, notify or ""
    except Exception:
        return None, ""  # fail open on any breaker/judge failure


def main() -> int:
    input_data = read_stdin_json()

    tool_name = str(input_data.get("tool_name") or "")
    tool_input = input_data.get("tool_input") or {}
    cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))

    # --- Overconfidence/groundedness breaker (runs on EVERY tool; judge debounced
    #     to <=1 call / 15s per session+prompt). Blocks ONLY mutation tools when
    #     gpt-realtime-2 flags a confident unproven claim; reads/web stay free.
    #     Whitelisted research Bash (ls/glob/rg/trace.sh/spec CLI) still passes. ---
    breaker_block, breaker_notify = _enforce_breaker(input_data)
    if breaker_block is not None:
        if tool_name == "Bash":
            command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
            allowed, _ = is_allowed_research_bash(command)
            if allowed:
                breaker_block = None
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
                "`unifable` (restate / add-task / dispute), never by hand-editing the JSON. ledger, goals, "
                "findings, and state are off-limits too."
            )

        rc = _enforce_spec(input_data, cwd)
        if rc == 0:
            return _emit_allow(breaker_notify)
        return rc

    # --- Bash: research whitelist (unconditional) ---
    if tool_name == "Bash":
        rc = _enforce_bash(input_data, tool_input, cwd)
        if rc == 0:
            return _emit_allow(breaker_notify)
        return rc

    # --- Delegation: locked until the same evidence spec unlocks action phase ---
    if tool_name in DELEGATION_TOOLS:
        rc = _enforce_delegation(input_data, tool_name, cwd)
        if rc == 0:
            return _emit_allow(breaker_notify)
        return rc

    # Any other tool — nothing to gate (read/search/web stay free).
    return _emit_allow(breaker_notify)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — fail open
        emit_json({})
        print(f"unifable pre-tool hook failed open: {exc}", file=sys.stderr)
        raise SystemExit(0)
