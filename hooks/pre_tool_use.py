#!/usr/bin/env python3
"""unifable pre-edit enforcement gate — PreToolUse.

Intercepts write tools (Edit / Write / MultiEdit / NotebookEdit / apply_patch),
Bash, and delegation tools (Task / Agent), and exits with code 2 (block) in
four cases:

  1. PROTECTED_PATHS: the target path resolves inside <cwd>/.unifable/ or under
     the global keyed spec store (<data_root>/specs/). Specs are CLI-only, so this
     prevents the model from modifying the spec, ledger state, findings, or
     any other gate-internal artifact with Edit/Write.

  2. EVIDENCE GATE — writes (unconditional): unless the effective grade is LIGHT,
     a valid spec carrying citation evidence (repo_context {cite, why},
     acceptance_criteria with live output, prior_art {cite, why} — all at STANDARD+) must
     exist for the current task before any edit is allowed. The spec is auto-created
     by the prompt hook and driven via the spec.py CLI (the no-brick escape), never
     hand-written.

  3. EVIDENCE GATE — shell research whitelist (unconditional): in the research
     phase (grade STANDARD+, no valid spec yet), Bash/REPL/exec_command may run only
     `cd`, `ls`, `glob`, `rg`, read-only file inspection (`head`, `tail`, `wc`,
     `sort`, `uniq`), read-only `git` subcommands and workflow git (`status`, `add`,
     `commit`, `push` without `--force`), a file whose basename is `trace.sh` or
     `websearch.sh` when the explore
     skill is installed (guidance shows resolved paths), or a user-facing unifusion skill
     script (`unifusion.sh`, `save_run.sh`, `summarize_session.sh`, `resolve_session.sh`).
     A valid spec unlocks the action phase (all shell commands allowed). LIGHT waives.
     Classification: scripts/gate/bash_classify.py.

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

from bash_classify import blocked_agent_env_reason, is_allowed_research_bash
from citations import format_citation_verify_message
from evidence_policy import resolve_evidence_profile, resolve_grade
from heavy_workflow import (
    compute_heavy_phase,
    edit_targets_primary_scope,
    heavy_declare_complete,
    heavy_workflow_brief,
    heavy_workflow_phase_hint,
)
from ledger import emit_json, load_ledger, read_stdin_json, update_ledger
from pretool_block import (
    block_context,
    block_epoch,
    consume_gate_cleared_notify,
    emit_pretool_block,
    format_bash_policy_block,
    format_bash_research_block,
    format_delegation_block,
    format_spec_missing_block,
    is_redundant_with_notify,
    normalize_bash_detail,
)
from protected_paths import (
    _BASH_EXTRA_MUTATE_RE,
    _bash_protected_write,
    _is_protected,
    _write_targets,
)
from spec_contracts import contract_string, format_spec_validation_block
from spec_io import canonical_project_root, load_spec, resolve_session_id, save_spec
from spec_validation import validate_spec
from tool_restrictions import (
    DELEGATION_TOOLS as _DELEGATION_TOOLS,
    WRITE_TOOLS as _WRITE_TOOLS,
    groundedness_block_message,
)

# ---------------------------------------------------------------------------
# Tool names across both hosts (Claude Code and Codex)
# ---------------------------------------------------------------------------

WRITE_TOOLS = frozenset(_WRITE_TOOLS)
DELEGATION_TOOLS = frozenset(_DELEGATION_TOOLS)

_PROTECTED_CLI = (
    "Specs/ledger are CLI-only via `unifable` "
    "(restate / add-task / set-primary / add-frontier / dispute); "
    "never hand-edit the JSON."
)


def _protected_path_message(path: str, *, shell: bool = False) -> str:
    base = f"Protected unifable state '{path}' — {_PROTECTED_CLI}"
    if shell:
        return base + " Shell redirects, sed, rm, tee, and apply_patch heredocs are not allowed."
    return base


def _heavy_block_suffix(input_data: dict, spec: dict, phase: str) -> str:
    try:
        ledger = load_ledger(input_data)
        if ledger.get("heavy_brief_injected"):
            return "\n" + heavy_workflow_phase_hint(spec, phase)
    except Exception:
        pass
    return "\n" + heavy_workflow_brief(spec, phase)

# ---------------------------------------------------------------------------
# Protected paths: the repo-local <cwd>/.unifable/ AND the global keyed spec store
# (<data_root>/specs/). Specs are CLI-only (spec.py) -- never model-writable.
# ---------------------------------------------------------------------------







# ---------------------------------------------------------------------------
# Extract the target file path from tool input
# ---------------------------------------------------------------------------




# apply_patch envelopes carry their target paths in the patch text, not in a
# stable key. Codex uses "*** Update File: <path>" header lines (and Add/Delete/
# Move); git-style patches use "--- a/<path>" / "+++ b/<path>". The patch text
# itself can live under different keys per host, so we concatenate EVERY string
# value found in tool_input (host-shape-robust) and scan that.
# Two header families: Codex "*** Update/Add/Delete File: <path>" plus the
# "*** Move to:/Move from: <path>" rename lines (which carry no "File:" token),
# and git-unified "--- a/<path>" / "+++ b/<path>".






# Shell mutations MUTATING_BASH_RE misses: output redirects (`>`, `>>`), in-place
# editors (`sed -i`, `perl -i`), and `tee`. Used only to decide whether to scan a
# command's tokens for protected targets — conservative by design.




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


def _plan_mode_state(input_data: dict) -> dict:
    try:
        from plan_mode import resolve_plan_mode_for_hooks

        return resolve_plan_mode_for_hooks(input_data)
    except Exception:
        return {"enabled": False, "host": "", "marker": ""}


def _block(
    input_data: dict,
    *,
    kind: str,
    detail: str,
    message: str,
    breaker_notify: str = "",
) -> int:
    msg = str(message or "").strip()
    if is_redundant_with_notify(msg, breaker_notify):
        msg = ""
    try:
        from plan_mode import append_plan_mode_note, pretool_should_append_plan_note

        plan = _plan_mode_state(input_data)
        if pretool_should_append_plan_note(input_data, plan):
            msg = append_plan_mode_note(msg, plan)
    except Exception:
        pass
    rc = emit_pretool_block(input_data, kind=kind, detail=detail, full_message=msg)
    if breaker_notify and breaker_notify.strip():
        print(breaker_notify.strip(), file=sys.stderr)
    return rc


def _citation_reasons(spec: dict, input_data: dict, cwd: str, require_commands: bool) -> list[str]:
    """Reasons the spec's citations are not backed by real session tool activity.
    Empty when the cross-check is disabled or anything fails (fail open)."""
    try:
        from citations import (
            activity_from_ledger,
            enabled,
            filter_gate_defect_citation_reasons,
            verify_citations,
        )

        if not enabled():
            return []
        ledger = load_ledger(input_data)
        if resolve_evidence_profile(ledger, spec) == "operational":
            return []
        activity = activity_from_ledger(ledger)
        reasons = verify_citations(spec, activity, cwd, require_commands=require_commands)
        return filter_gate_defect_citation_reasons(spec, reasons, cwd)
    except Exception:
        return []


def _run_spec_hygiene(input_data: dict, cwd: str) -> list[str]:
    """Deterministic spec hygiene before gate checks. Returns headlines."""
    if _effective_grade(input_data) == "LIGHT":
        return []
    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    if spec is None:
        return []
    try:
        from citations import activity_from_ledger
        from spec_hygiene import apply_spec_hygiene

        changed, headlines = apply_spec_hygiene(
            spec,
            activity_from_ledger(load_ledger(input_data)),
            cwd,
        )
        if changed:
            save_spec(cwd, task_id, spec)
        return headlines
    except Exception:
        return []


def _allow_notify(input_data: dict, breaker_notify: str, hygiene_headlines: list[str]) -> int:
    cleared = consume_gate_cleared_notify(input_data, hygiene_headlines)
    parts = [p.strip() for p in (breaker_notify, cleared) if p and p.strip()]
    return _emit_allow("\n".join(parts))


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


def _evidence_profile(input_data: dict | None, spec: dict | None) -> str:
    ledger: dict = {}
    if input_data is not None:
        try:
            ledger = load_ledger(input_data)
        except Exception:
            ledger = {}
    return resolve_evidence_profile(ledger, spec if isinstance(spec, dict) else None)


def _enforce_heavy_writes(input_data: dict, spec: dict, cwd: str, target: str | None) -> int:
    """HEAVY frontier-first phase gates on write tools after spec validates."""
    phase = compute_heavy_phase(spec)
    if phase == "declare" or not heavy_declare_complete(spec):
        return _block(
            input_data,
            kind="heavy",
            detail="declare",
            message=(
                "HEAVY declare phase: research only — no edits until restated goal, "
                "citation evidence, >=2 frontier tasks, and 1 primary task exist."
                + _heavy_block_suffix(input_data, spec, phase)
            ),
        )
    if phase == "frontier" and target and edit_targets_primary_scope(spec, target, cwd):
        return _block(
            input_data,
            kind="heavy",
            detail="frontier",
            message=(
                "HEAVY frontier phase: primary approach is blocked. Explore and implement "
                "ALL frontier approaches first -- the judge adjudicates each on Stop "
                "(rejected/still_viable/accepted). When all are explored, the judge "
                "compares evidence and may adopt the best frontier over primary."
                + _heavy_block_suffix(input_data, spec, phase)
            ),
        )
    return 0


def _enforce_spec(
    input_data: dict,
    cwd: str,
    *,
    write_target: str | None = None,
    breaker_notify: str = "",
) -> int:
    """Block a write tool unless a valid evidence spec exists for the task.

    The evidence gate is unconditional — there is no env disable. A valid spec
    carrying citation evidence (repo_context {cite, why}, acceptance_criteria with
    live output, prior_art {cite, why}) must exist for any STANDARD+ task. LIGHT waives."""
    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    ctx = block_context(input_data)
    if spec is None:
        contract = contract_string(grade, True, _evidence_profile(input_data, None))
        return _block(
            input_data,
            kind="spec",
            detail=f"missing:{grade}",
            message=format_spec_missing_block(grade, task_id, contract, ctx=ctx),
            breaker_notify=breaker_notify,
        )

    profile = _evidence_profile(input_data, spec)
    ok, reasons = validate_spec(spec, grade, require_evidence=True, evidence_profile=profile)
    if not ok:
        detail = "; ".join(reasons)
        try:
            _led = load_ledger(input_data)
            _epoch = block_epoch(input_data, _led)
            _include_contract = _led.get("spec_contract_notified_epoch") != _epoch
            _scaffold_notified = bool(_led.get("prompt_scaffold_notified"))
        except Exception:
            _include_contract = True
            _scaffold_notified = False
        message = format_spec_validation_block(
            grade,
            reasons,
            profile,
            spec,
            include_contract=_include_contract,
            scaffold_notified=_scaffold_notified or ctx.scaffold_notified,
            contract_notified=ctx.contract_notified,
        )
        if _include_contract:
            try:

                def _mark_contract(ld):
                    ld["spec_contract_notified_epoch"] = block_epoch(input_data, ld)

                update_ledger(input_data, _mark_contract)
            except Exception:
                pass
        return _block(
            input_data,
            kind="spec",
            detail=detail,
            message=message,
            breaker_notify=breaker_notify,
        )

    cited = _citation_reasons(spec, input_data, cwd, require_commands=False)
    if cited:
        detail = "; ".join(cited)
        return _block(
            input_data,
            kind="spec",
            detail=f"citations:{detail}",
            message=format_citation_verify_message(
                cited,
                include_footnotes=not ctx.scaffold_notified,
            ),
            breaker_notify=breaker_notify,
        )

    if grade == "HEAVY":
        rc = _enforce_heavy_writes(input_data, spec, cwd, write_target)
        if rc != 0:
            return rc

    return 0


def _lift_goal(input_data: dict, cwd: str) -> str:
    """Best-effort user goal for the lift judge: spec restated goal, else prompt."""
    try:
        spec = load_spec(cwd, _task_id(input_data))
        if isinstance(spec, dict):
            goal = str(spec.get("restated_goal") or "").strip()
            if goal:
                return goal
    except Exception:
        pass
    try:
        ledger = load_ledger(input_data)
        return str(ledger.get("prompt") or ledger.get("operative_prompt") or "").strip()
    except Exception:
        return ""


def _is_mutating_bash(command: str | None) -> bool:
    """True when a shell command actually mutates (cp/mv/rm/touch/redirect/...).

    The lift only applies to blocked MUTATIONS the gate is wrongly stopping. A
    non-whitelisted READ (cat/nl/pwd/echo) keeps its deterministic research-phase
    block -- the agent should use Read/Grep/Glob or a whitelisted command -- so it
    must never trigger a lift judge call. Mirrors `_bash_protected_write`'s test."""
    cmd = command or ""
    if not cmd:
        return False
    try:
        from parse_tool_result import MUTATING_BASH_RE
    except Exception:
        return False
    return bool(MUTATING_BASH_RE.search(cmd)) or bool(_BASH_EXTRA_MUTATE_RE.search(cmd))


def _evidence_lift_allows(
    input_data: dict,
    tool_name: str,
    cwd: str,
    *,
    command: str | None = None,
    paths: list[str] | None = None,
) -> bool:
    """Judge-granted lift of the research-phase evidence block (fail-closed).

    Called ONLY when the deterministic gate is about to block a mutation, and ONLY
    after the absolute guards (PROTECTED_PATHS, dangerous-env) have already passed,
    so a lift can never open those. Returns True to allow this exact action: either
    a still-budgeted prior grant covers it, or the judge approves it now on this
    same PreToolUse call (seamless -- the main model never sees a block or retries).
    Any error or denial returns False, leaving today's block in place."""
    try:
        from breaker_state import load_breaker, save_breaker
        from gate_lift import (
            bump_judge_calls,
            consume_lift,
            judge_budget_left,
            judge_gate_lift,
            lift_allows,
            lift_from_state,
            record_lift,
        )

        state = load_breaker(input_data)
        if lift_allows(lift_from_state(state), tool_name, command, paths):
            consume_lift(state)
            save_breaker(input_data, state)
            return True
        if not judge_budget_left(state):
            return False
        decision = judge_gate_lift(
            goal=_lift_goal(input_data, cwd),
            tool_name=tool_name,
            command=command or "",
            paths=paths or [],
        )
        bump_judge_calls(state)
        if int(decision.get("lift", 0) or 0) == 1:
            scope = str(decision.get("scope") or "")
            record_lift(state, tool_name, command, paths, scope)
            consume_lift(state)
            save_breaker(input_data, state)
            print(
                f"Evidence gate lifted — {scope}"
                if scope
                else "Evidence gate lifted for this step.",
                file=sys.stderr,
            )
            return True
        save_breaker(input_data, state)
        return False
    except Exception:
        return False


def _enforce_bash(
    input_data: dict,
    tool_input: dict,
    cwd: str,
    *,
    tool_name: str = "Bash",
    breaker_notify: str = "",
) -> int:
    """Research-phase whitelist for shell tools (Bash, exec_command, REPL).

    Research phase (no valid spec): allow only cd, ls, glob, rg, trace.sh, websearch.sh,
    and the user-facing unifusion skill scripts so the agent can explore or run a panel
    before unlock. Action phase (valid spec): all shell commands are allowed.
    LIGHT waives entirely."""
    try:
        from parse_tool_result import (
            _REPL_CAT_RE,
            _REPL_READ_PATH_RE,
            command_from_input,
            is_repl_tool,
            repl_code_from_input,
            repl_shell_cmds_from_code,
        )
    except ImportError:
        from scripts.gate.parse_tool_result import (
            _REPL_CAT_RE,
            _REPL_READ_PATH_RE,
            command_from_input,
            is_repl_tool,
            repl_code_from_input,
            repl_shell_cmds_from_code,
        )

    ctx = block_context(input_data)

    if is_repl_tool(tool_name):
        code = repl_code_from_input({"tool_name": tool_name, "tool_input": tool_input})
        bash_cmds = repl_shell_cmds_from_code(code)
        if bash_cmds:
            for cmd in bash_cmds:
                blocked_env = blocked_agent_env_reason(cmd)
                if blocked_env:
                    return _block(
                        input_data,
                        kind="bash",
                        detail=normalize_bash_detail(blocked_env),
                        message=format_bash_policy_block(blocked_env, _task_id(input_data), ctx=ctx),
                    )
        elif not (_REPL_READ_PATH_RE.search(code) or _REPL_CAT_RE.search(code)):
            return _block(
                input_data,
                kind="bash",
                detail="repl-not-research",
                message=format_bash_research_block(
                    "REPL code is not a whitelisted research read",
                    _task_id(input_data),
                    ctx=ctx,
                ),
            )
        command = bash_cmds[0] if bash_cmds else ""
    else:
        command = command_from_input({"tool_name": tool_name, "tool_input": tool_input})
        blocked_env = blocked_agent_env_reason(command)
        if blocked_env:
            return _block(
                input_data,
                kind="bash",
                detail=normalize_bash_detail(blocked_env),
                message=format_bash_policy_block(blocked_env, _task_id(input_data), ctx=ctx),
            )

    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    profile = _evidence_profile(input_data, spec)
    if spec is not None:
        ok, _ = validate_spec(spec, grade, require_evidence=True, evidence_profile=profile)
        if ok and not _citation_reasons(spec, input_data, cwd, require_commands=False):
            return 0  # action phase unlocked

    if is_repl_tool(tool_name):
        for cmd in bash_cmds:
            allowed, why = is_allowed_research_bash(cmd)
            if not allowed:
                if _is_mutating_bash(cmd) and _evidence_lift_allows(input_data, tool_name, cwd, command=cmd):
                    continue
                return _block(
                    input_data,
                    kind="bash",
                    detail=normalize_bash_detail(why),
                    message=format_bash_research_block(why, task_id, ctx=ctx),
                )
        return 0

    allowed, why = is_allowed_research_bash(command)
    if not allowed:
        if _is_mutating_bash(command) and _evidence_lift_allows(input_data, tool_name, cwd, command=command):
            return 0
        return _block(
            input_data,
            kind="bash",
            detail=normalize_bash_detail(why),
            message=format_bash_research_block(why, task_id, ctx=ctx),
        )

    return 0


def _shell_research_passes(input_data: dict, tool_name: str, tool_input: dict) -> bool:
    """True when a shell/REPL call is allowed research (breaker bypass)."""
    try:
        from parse_tool_result import (
            _REPL_CAT_RE,
            _REPL_READ_PATH_RE,
            command_from_input,
            is_repl_tool,
            is_shell_tool,
            repl_code_from_input,
            repl_shell_cmds_from_code,
        )
    except ImportError:
        from scripts.gate.parse_tool_result import (
            _REPL_CAT_RE,
            _REPL_READ_PATH_RE,
            command_from_input,
            is_repl_tool,
            is_shell_tool,
            repl_code_from_input,
            repl_shell_cmds_from_code,
        )

    if not is_shell_tool(tool_name):
        return False
    if is_repl_tool(tool_name):
        code = repl_code_from_input({"tool_name": tool_name, "tool_input": tool_input})
        bash_cmds = repl_shell_cmds_from_code(code)
        if bash_cmds:
            return all(is_allowed_research_bash(cmd)[0] for cmd in bash_cmds)
        return bool(_REPL_READ_PATH_RE.search(code) or _REPL_CAT_RE.search(code))
    allowed, _ = is_allowed_research_bash(command_from_input({"tool_name": tool_name, "tool_input": tool_input}))
    return allowed


def _enforce_delegation(input_data: dict, tool_name: str, cwd: str, *, breaker_notify: str = "") -> int:
    """Block Task/Agent until a valid evidence spec unlocks the action phase."""
    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        return 0

    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    profile = _evidence_profile(input_data, spec)
    if spec is not None:
        ok, _ = validate_spec(spec, grade, require_evidence=True, evidence_profile=profile)
        if ok and not _citation_reasons(spec, input_data, cwd, require_commands=False):
            return 0

    return _block(
        input_data,
        kind="delegate",
        detail=tool_name,
        message=format_delegation_block(tool_name, task_id, ctx=block_context(input_data)),
        breaker_notify=breaker_notify,
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

        from breaker_orchestration import evaluate_pre_tool_locked

        ledger = load_ledger(input_data)
        active = str(ledger.get("active_task") or "")
        # Locked load->judge->save: a parallel tool-call batch coalesces to one
        # judge API call instead of one per concurrent PreToolUse process.
        block, steering, notify, breaker = evaluate_pre_tool_locked(input_data, time.time(), active)
        if block:
            events = breaker.get("events") if isinstance(breaker.get("events"), list) else []
            if events and events[-1].get("kind") == "REINSTATE" and not steering:
                pass
            message = groundedness_block_message(steering)
            detail = " ".join(str(message).split())[:80]
            return _block(
                input_data,
                kind="breaker",
                detail=detail,
                message=message,
            ), ""
        return None, notify or ""
    except Exception:
        return None, ""  # fail open on any breaker/judge failure


def _is_gated_tool(tool_name: str) -> bool:
    """True when tool_name is one the PreToolUse hook gates (writes, delegation, or
    a shell tool). The director scope only restricts among these; everything else
    (reads, web, empty/unknown) is never scope-blocked."""
    if tool_name in WRITE_TOOLS or tool_name in DELEGATION_TOOLS:
        return True
    try:
        from parse_tool_result import is_shell_tool
    except ImportError:  # pragma: no cover
        from scripts.gate.parse_tool_result import is_shell_tool
    return bool(is_shell_tool(tool_name))


def _spec_validated(input_data: dict, cwd: str) -> bool:
    """True when the evidence spec for this task is validated (action phase).

    Mirrors the unlock check in _enforce_delegation: LIGHT is always unlocked; a
    STANDARD+ task is unlocked once its spec validates with backed citations."""
    grade = _effective_grade(input_data)
    if grade == "LIGHT":
        return True
    task_id = _task_id(input_data)
    spec = load_spec(cwd, task_id)
    if spec is None:
        return False
    profile = _evidence_profile(input_data, spec)
    ok, _ = validate_spec(spec, grade, require_evidence=True, evidence_profile=profile)
    return bool(ok) and not _citation_reasons(spec, input_data, cwd, require_commands=False)


def _enforce_tool_scope(input_data: dict, tool_name: str, breaker_notify: str) -> int | None:
    """Block an out-of-scope tool per the director's persisted scope. Fail-open.

    Reads the just-persisted breaker state (no judge call) and runs the pure
    tool_scope predicate. The director's HARD tool-gating is subordinate to the
    evidence gate: it applies only while the spec is unvalidated (the research /
    grounding phase, where it keeps the agent reading and restating). Once the
    spec validates (action phase), the director is advisory -- the breaker still
    guards overconfidence, but the director must not override a legitimately
    unlocked edit. Returns a blocking exit code when out of scope, else None."""
    try:
        from breaker_state import load_breaker
        from tool_scope import in_scope, scope_from_state

        # Only enforce scope for tools this hook actually gates (writes, delegation,
        # shell). An empty or unknown tool (e.g. malformed/empty stdin) must never be
        # scope-blocked -- reads/web stay free via the grounding floor anyway.
        if not _is_gated_tool(tool_name):
            return None
        # Evidence-gathering shell never gets scope-blocked: a content-revealing
        # search (grep/rg/ast-grep/read-only inspection) grounds a claim exactly as a
        # Read does, and the spec CLI must stay reachable to progress the gate. Mirror
        # the breaker's research bypass (main() / _shell_research_passes) so the
        # director steers mutations, never the agent's research.
        tool_input = input_data.get("tool_input") or {}
        if _shell_research_passes(input_data, tool_name, tool_input):
            return None
        state = load_breaker(input_data)
        scope = scope_from_state(state)
        if not scope:
            return None
        ok, reason = in_scope(tool_name, scope)
        if ok:
            return None
        cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))
        if _spec_validated(input_data, cwd):
            return None  # action phase: director is advisory, not blocking
        # The scope block's reason already carries the directive; drop a breaker_notify
        # that merely echoes it (the "unifable director: <directive>" pending notify)
        # so the directive surfaces once, not duplicated across both stderr channels.
        notify = "" if (breaker_notify and reason and reason in breaker_notify) else breaker_notify
        return _block(
            input_data, kind="scope", detail=tool_name, message=reason, breaker_notify=notify
        )
    except Exception:
        return None  # fail open on any error


def main() -> int:
    input_data = read_stdin_json()

    try:
        from judge_transport import bind_session

        bind_session(input_data)
    except Exception:
        pass

    tool_name = str(input_data.get("tool_name") or "")
    tool_input = input_data.get("tool_input") or {}
    cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))

    # --- Overconfidence/groundedness breaker (runs on EVERY tool; judge debounced
    #     to <=1 call / 15s per session+prompt). Blocks ONLY mutation tools when
    #     gpt-realtime-2 flags a confident unproven claim; reads/web stay free.
    #     Whitelisted research Bash (cd/ls/glob/rg/trace.sh/websearch.sh/unifusion scripts/spec CLI) still passes. ---
    breaker_block, breaker_notify = _enforce_breaker(input_data)
    if breaker_block is not None:
        if _shell_research_passes(input_data, tool_name, tool_input):
            breaker_block = None
        if breaker_block is not None:
            return breaker_block

    # --- Per-step tool scope (deterministic; no judge call) ---
    #     The director judge persisted a tool scope on its last debounced call.
    #     Enforce it here with a pure predicate: an out-of-scope mutation/bash/
    #     delegation tool is blocked with the director's directive as the reason.
    #     Reads/web never reach this hook (matcher), and the grounding floor in
    #     tool_scope.in_scope keeps them reachable regardless. Fail-open on any error.
    scope_block = _enforce_tool_scope(input_data, tool_name, breaker_notify)
    if scope_block is not None:
        return scope_block

    # --- Write tools: protected paths + evidence gate (unconditional) ---
    if tool_name in WRITE_TOOLS:
        hygiene_headlines = _run_spec_hygiene(input_data, cwd)
        targets = _write_targets(tool_name, tool_input)
        target = targets[0] if targets else None

        # Guard 1: PROTECTED_PATHS (includes .unifable/spec/* — specs are CLI-only).
        # apply_patch can touch several files in one envelope, so check them all.
        protected_hit = next((t for t in targets if _is_protected(t, cwd)), None)
        if protected_hit:
            return _block(
                input_data,
                kind="protected",
                detail="write",
                message=_protected_path_message(protected_hit),
                breaker_notify=breaker_notify,
            )

        rc = _enforce_spec(
            input_data, cwd, write_target=target, breaker_notify=breaker_notify
        )
        if rc == 0:
            return _allow_notify(input_data, breaker_notify, hygiene_headlines)
        if _evidence_lift_allows(input_data, tool_name, cwd, paths=targets):
            return _allow_notify(input_data, breaker_notify, hygiene_headlines)
        return rc

    # --- Shell tools: research whitelist (unconditional) ---
    try:
        from parse_tool_result import (
            command_from_input,
            is_repl_tool,
            is_shell_tool,
            repl_code_from_input,
            repl_shell_cmds_from_code,
        )
    except ImportError:
        from scripts.gate.parse_tool_result import (
            command_from_input,
            is_repl_tool,
            is_shell_tool,
            repl_code_from_input,
            repl_shell_cmds_from_code,
        )

    if is_shell_tool(tool_name):
        if is_repl_tool(tool_name):
            code = repl_code_from_input({"tool_name": tool_name, "tool_input": tool_input})
            shell_cmds = repl_shell_cmds_from_code(code)
        else:
            shell_cmds = [command_from_input({"tool_name": tool_name, "tool_input": tool_input})]

        for command in shell_cmds:
            protected_hit = _bash_protected_write(command, cwd)
            if protected_hit:
                return _block(
                    input_data,
                    kind="protected",
                    detail="bash",
                    message=_protected_path_message(protected_hit, shell=True),
                    breaker_notify=breaker_notify,
                )

        hygiene_headlines = _run_spec_hygiene(input_data, cwd)
        rc = _enforce_bash(
            input_data, tool_input, cwd, tool_name=tool_name, breaker_notify=breaker_notify
        )
        if rc == 0:
            return _allow_notify(input_data, breaker_notify, hygiene_headlines)
        return rc

    # --- Delegation: locked until the same evidence spec unlocks action phase ---
    if tool_name in DELEGATION_TOOLS:
        hygiene_headlines = _run_spec_hygiene(input_data, cwd)
        rc = _enforce_delegation(input_data, tool_name, cwd, breaker_notify=breaker_notify)
        if rc == 0:
            return _allow_notify(input_data, breaker_notify, hygiene_headlines)
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
        print(f"Pre-tool hook failed open: {exc}", file=sys.stderr)
        raise SystemExit(0)
