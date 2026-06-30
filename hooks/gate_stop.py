#!/usr/bin/env python3
"""unifable completion gate — Stop.

Blocks completion in priority order; fails open on malformed input:

  1. Evidence gate (INFINITE, no env disable): on a non-LIGHT task the evidence
     spec must EXIST and validate before finishing (restated_goal,
     acceptance_criteria with live output, repo_context {cite,why}, prior_art {cite,why}).
     No spec, or a placeholder/invalid one, blocks EVERY stop — ignoring the loop
     guard (stop_hook_active) and the stop-block cap — until a valid spec exists.
     The agent is unconditionally required to write its evidence back. Releases
     only on a valid spec, LIGHT grade, no session_id (fail open), the holdout
     'off' arm, or a gate exception (fail open).
  2. Completion handoff judge: gpt-realtime-2 blocks when the last text-only turn
     defers autonomous work (permission-seeking, "say the word and I'll…", dangling
     follow-ups). Bypasses stop_hook_active; capped at COMPLETION_HANDOFF_BLOCK_CAP.
  3. Loop guard for softer gates below (findings + observation): never block twice
     in a row on the same stop.
  4. Findings cross-link (opt-in).
  5. Observation gate: changed-but-unverified on HEAVY tasks. Capped at MAX_STOP_BLOCKS,
     behind stop_hook_active, so it never traps.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "gate"))
sys.path.insert(0, str(_HERE.parent / "scripts" / "shadow"))

from evidence_policy import resolve_evidence_profile, resolve_grade
from ledger import emit_json, load_ledger, read_stdin_json, save_ledger
from transcript_locate import locate_transcript
from verify_state import (
    MAX_STOP_BLOCKS,
    completion_runaway_warning,
    note_completion_block,
    reset_completion_stall,
    should_block_stop,
    warning_after_max_blocks,
)

# Wall-clock budget for the judge/check work this Stop hook does, kept under the
# host Stop-hook timeout (hooks.json wires gate_stop at 120s). auto_validate_spec
# stops launching work past this deadline so the hook always returns cleanly
# instead of being killed mid-judge (the codex-thread 10s timeout).
try:
    STOP_JUDGE_BUDGET = float(os.environ.get("UNIFABLE_STOP_BUDGET", "100"))
except ValueError:
    STOP_JUDGE_BUDGET = 100.0
# Advisory-hint loop for the completion breaker. After the agent has re-blocked
# Stop this many times it is plausibly stuck, so the judge offers ONE concrete
# next step. The hint never lifts the gate -- it rides alongside the block reason.
COMPLETION_HINT_THRESHOLD = 3
COMPLETION_HINT_STEP = 3  # re-offer a nudge every N blocks past the threshold


def _completion_stop_hint(input_data: dict, spec: dict, incomplete: list[str]) -> str:
    """Advisory nudge for an agent stuck behind the completion breaker.

    Reads the persistent consecutive-block counter (owned by
    note_completion_block) to decide when to offer a hint. Once it crosses the
    threshold (and every STEP blocks after), spends one judge call for a concrete
    next step. Returns hint text to append to the still-blocking reason, or ""
    when it is not time to nudge or the judge is silent. NEVER lifts the gate;
    NEVER mutates completion_stop_blocks (single-writer ownership is in
    note_completion_block); fails open."""
    try:
        ledger = load_ledger(input_data)
        count = int(ledger.get("completion_stop_blocks") or 0)
        if count < COMPLETION_HINT_THRESHOLD or (count - COMPLETION_HINT_THRESHOLD) % COMPLETION_HINT_STEP != 0:
            return ""
        from spec_judge import judge_hint

        recent = " | ".join(
            (ledger.get("ran_commands") or [])[-6:] + [f"failure:{f}" for f in (ledger.get("failures") or [])[-3:]]
        )
        signal = (
            f"The completion breaker has re-blocked Stop {count} times; "
            f"{len(incomplete)} requirement(s) still not validated "
            f"({', '.join(incomplete)}). The agent may be looping without converging."
        )
        return judge_hint(spec, signal=signal, recent=recent)
    except Exception:
        return ""  # fail open -- a hint never blocks


def _attach_validate_context(payload: dict, ctx: str) -> None:
    """Attach the Stop-time spec board to additionalContext on block payloads only.

    On Stop, additionalContext continues the conversation (Claude Code docs).
    Use only when ``decision: block`` is set; allow-stop paths must emit ``{}``
    or ``systemMessage`` alone. The short blocking alarm stays in ``reason``.
    """
    try:
        from hook_output import attach_stop_validate_context

        attach_stop_validate_context(payload, ctx)
    except Exception:
        if not ctx or not ctx.strip():
            return
        hso = payload.setdefault("hookSpecificOutput", {})
        hso["hookEventName"] = "Stop"
        existing = str(hso.get("additionalContext") or "").strip()
        hso["additionalContext"] = f"{existing}\n{ctx}".strip() if existing else ctx


def _advance_release(input_data: dict) -> str:
    """Stop-barrier convergence for the async breaker-release (disarm) lane.

    Two parts, both fail-open:
      1. Drain any pending disarm message a detached worker already enqueued
         (read-and-clear), so a lift that landed after the last gated tool still
         surfaces at end of turn.
      2. If the breaker is STILL armed (e.g. the model went text-only, so no
         release tool fired in PostToolUse and no worker was dispatched), run the
         release judge synchronously under breaker_lock here -- Stop is the only
         barrier that reliably fires on a text-only tail. Mirrors the former inline
         PostToolUse disarm, now consolidated to the one place it is load-bearing.
    Never blocks or raises into the Stop path."""
    try:
        from breaker_release_lane import drain_pending_release

        drained = drain_pending_release(input_data) or ""
    except Exception:
        drained = ""

    converged = ""
    try:
        import time as _time

        from breaker_orchestration import evaluate_post_tool_release
        from breaker_state import breaker_lock, load_breaker, save_breaker
        from ledger import load_ledger

        with breaker_lock(input_data):
            state = load_breaker(input_data)
            if state.get("breaker_armed") or state.get("breaker_provisional"):
                # No fresh tool block on a text-only tail; the release judge reads the
                # transcript segment. verify_key lane is handled by _advance_auto_verify.
                if not state.get("breaker_verify_key"):
                    active_task = ""
                    try:
                        active_task = str((load_ledger(input_data) or {}).get("active_task") or "")
                    except Exception:
                        active_task = ""
                    _grounded, _needed, msg = evaluate_post_tool_release(
                        input_data, state, fresh_tool="", active_task=active_task
                    )
                    save_breaker(input_data, state)
                    converged = str(msg or "")
        _ = _time  # imported for parity with _advance_auto_verify; no sleep here
    except Exception:
        converged = ""

    parts = [p.strip() for p in (drained, converged) if p and p.strip()]
    return "\n".join(parts)


def _advance_auto_verify(input_data: dict) -> str:
    """Stop-parity for the breaker's async auto-grounding lane: poll any dispatched
    background verification and persist the result so a model that dispatched then
    went text-only still gets disarmed/confirmed at end of turn (Stop is the only
    other hook event that reliably fires). Returns a terse note to surface, or ''.
    Fully fail-open -- never blocks or raises into the Stop path."""
    try:
        import time as _time

        from breaker_orchestration import _poll_auto_verify
        from breaker_state import breaker_lock, load_breaker, save_breaker

        with breaker_lock(input_data):
            state = load_breaker(input_data)
            if not state.get("breaker_verify_key"):
                return ""
            note = _poll_auto_verify(input_data, state, _time.time())
            save_breaker(input_data, state)
        return str(note or "").strip()
    except Exception:
        return ""


def _director_continuation(input_data: dict) -> str:
    """The live director directive, for guided-iterative-continuation on a blocked
    Stop. A "turn" is one tool call; Stop is the rare moment the model thinks it is
    done. Handing back the current directive keeps the goal loop going with the same
    per-tool-call guidance. Fail-open to "" (a missing directive never blocks)."""
    try:
        from breaker_state import load_breaker
        from tool_scope import current_directive

        directive = current_directive(load_breaker(input_data)).strip()
        if not directive:
            return ""
        return directive
    except Exception:
        return ""


def _emit_stop_payload(
    payload: dict,
    input_data: dict,
    *,
    validate_ctx: str = "",
    loop_lift_ctx: str = "",
    digest_path: str = "",
) -> None:
    # Stop-prevention: on a blocked Stop, append the live director directive to the
    # continuation context so the rare end-of-session attempt becomes a guided
    # iterative continuation. Only on block -- a clean allow-stop must emit {} so it
    # does not re-engage the session.
    if payload.get("decision") == "block":
        directive_ctx = _director_continuation(input_data)
        if directive_ctx:
            loop_lift_ctx = _merge_reason_parts(loop_lift_ctx, directive_ctx)
    try:
        from hook_output import finalize_stop_payload

        payload = finalize_stop_payload(
            payload,
            validate_ctx=validate_ctx,
            loop_lift_ctx=loop_lift_ctx,
            input_data=input_data,
            digest_path=digest_path,
        )
    except Exception:
        _attach_validate_context(payload, _merge_reason_parts(validate_ctx, loop_lift_ctx))
    emit_json(payload)


def _merge_reason_parts(*parts: str) -> str:
    return "\n\n".join(p.strip() for p in parts if p and str(p).strip())


def _build_stop_validate_context(
    spec: dict | None,
    val_msgs: list[str],
    *,
    max_len: int | None = None,
) -> tuple[str, bool]:
    if spec is None or not val_msgs:
        return "", False
    try:
        from model_notify import build_stop_validate_context

        return build_stop_validate_context(spec, val_msgs, max_len=max_len)
    except Exception:
        return "", False


def _subagent_attribution(
    stop_added: dict,
    ledger_activity: dict,
    transcript_activity: dict,
    cwd: str,
) -> str:
    """Gap 7: name citations the Stop sync credited from sub-agent / transcript
    activity that the main model never performed directly. Fail-open to ""."""
    try:
        added = list(stop_added.get("repo_context") or []) + list(stop_added.get("prior_art") or [])
        if not added:
            return ""
        from citations import path_was_read, url_was_fetched

        ledger_reads = ledger_activity.get("read_paths") or []
        ledger_fetches = ledger_activity.get("fetched_urls") or []
        trans_reads = transcript_activity.get("read_paths") or []
        trans_fetches = transcript_activity.get("fetched_urls") or []
        credited: list[str] = []
        for cite in added:
            c = str(cite)
            is_url = c.startswith("http://") or c.startswith("https://")
            if is_url:
                in_ledger = url_was_fetched(c, ledger_fetches)
                in_trans = url_was_fetched(c, trans_fetches)
            else:
                in_ledger = path_was_read(c, ledger_reads, cwd)
                in_trans = path_was_read(c, trans_reads, cwd)
            if in_trans and not in_ledger:
                credited.append(c)
        if not credited:
            return ""
        shown = ", ".join(credited[:3]) + ("..." if len(credited) > 3 else "")
        return f"credited sub-agent/transcript activity for {len(credited)} citation(s): {shown}"
    except Exception:
        return ""


def _stop_workflow_notes(
    spec: dict,
    heavy_before: tuple[str, str],
    stop_added: dict,
    ledger_activity: dict,
    transcript_activity: dict,
    cwd: str,
) -> list[str]:
    """Gap 2 + 7: Stop-digest enrichments destined for val_msgs (block-only).

    Headlines for a HEAVY phase flip / primary unblock and for citations
    backfilled by sub-agent activity. Fail-open to []."""
    notes: list[str] = []
    try:
        from heavy_workflow import heavy_snapshot, heavy_transition_headline

        headline = heavy_transition_headline(heavy_before, heavy_snapshot(spec), spec)
        if headline:
            notes.append(headline)
    except Exception:
        pass
    note = _subagent_attribution(stop_added, ledger_activity, transcript_activity, cwd)
    if note:
        notes.append(note)
    return notes


def _persist_stop_digest(cwd: str, session_id: str | None, body: str) -> str:
    """Write full stop validation digest; return path or "" on failure."""
    if not body.strip() or not session_id:
        return ""
    try:
        from atomicio import write_text_atomic
        from spec_io import session_dir

        path = session_dir(cwd, session_id) / "last_stop_validation.txt"
        write_text_atomic(path, body)
        return str(path)
    except Exception:
        return ""


def _handle_completion_loop_release(
    input_data: dict,
    cwd: str,
    task_key: str,
    spec: dict,
    ledger: dict,
    incomplete: list[str],
    val_msgs: list[str],
    validate_ctx: str,
) -> tuple[dict, bool, list[str], list[str], str, dict | None]:
    """Loop signature, provisional consume, or loop judge. Returns updated state;
    early_payload when Stop is allowed via provisional lift."""
    from loop_release import (
        apply_loop_release_verdict,
        consume_provisional_stop_lift,
        judge_completion_loop_release,
        loop_lift_active,
        provisional_allow_message,
        should_invoke_loop_judge,
        update_loop_signature,
    )
    from spec_io import save_spec
    from spec_tasks import all_tasks_validated

    update_loop_signature(ledger, incomplete)

    if loop_lift_active(ledger) and consume_provisional_stop_lift(ledger):
        msg = provisional_allow_message(ledger)
        save_ledger(input_data, ledger)
        payload = {"systemMessage": msg}
        return spec, False, incomplete, val_msgs, validate_ctx, payload

    if should_invoke_loop_judge(ledger, incomplete, pending_block=True, spec=spec):
        recent = " | ".join(
            (ledger.get("ran_commands") or [])[-6:] + [f"failure:{f}" for f in (ledger.get("failures") or [])[-3:]]
        )
        signal = (
            f"Completion breaker blocked Stop with {len(incomplete)} incomplete task(s) "
            f"({', '.join(incomplete)}). stall_blocks={ledger.get('completion_stall_blocks')} "
            f"stop_blocks={ledger.get('completion_stop_blocks')} "
            f"same_set_streak={ledger.get('loop_same_set_streak')}."
        )
        verdict = judge_completion_loop_release(spec, ledger, signal=signal, recent=recent)
        headlines, _lift_msg = apply_loop_release_verdict(spec, ledger, verdict)
        # Provisional lifts are rendered in full by format_loop_lift_context
        # (loop_lift_ctx); re-adding the truncated announcement headline here would
        # double the lift in additionalContext (Notes echo). Permanent retractions
        # have no loop_lift_ctx, so their board headlines must still flow.
        if headlines and verdict.lift != "provisional":
            val_msgs = list(val_msgs) + headlines
            validate_ctx, _ = _build_stop_validate_context(spec, val_msgs)
        save_spec(cwd, task_key, spec)
        save_ledger(input_data, ledger)
        ok_tasks, incomplete = all_tasks_validated(spec)
        return spec, ok_tasks, incomplete, val_msgs, validate_ctx, None

    save_ledger(input_data, ledger)
    ok_tasks, incomplete = all_tasks_validated(spec)
    return spec, ok_tasks, incomplete, val_msgs, validate_ctx, None


def _holdout_suppresses(input_data: dict) -> bool:
    """M3 holdout: env-gated, opt-in. When UNIFABLE_HOLDOUT=1, sessions in the
    'off' arm skip the gate (pure baseline for measuring whether the gate
    helps). Default OFF -> gate behaviour is identical, so the 16/16 regression
    (which tests should_block_stop directly) is unaffected. The arm is computed
    out-of-band and is never shown to the model."""
    if os.environ.get("UNIFABLE_HOLDOUT") != "1":
        return False
    try:
        from shadow_logger import holdout_arm

        return holdout_arm(input_data.get("session_id") or "") == "off"
    except Exception:
        return False


def _log_holdout(input_data: dict, reason: str) -> None:
    """Record the holdout suppression out-of-band (events.jsonl). Never emits to
    the model and never raises into the gate path."""
    try:
        from shadow_logger import append_event, make_event

        append_event(make_event(input_data.get("session_id") or "", "holdout_suppress", {"would_block_reason": reason}))
    except Exception:
        pass


def ledger_grade(input_data: dict) -> str:
    """The effective enforcement grade for this session (env-blind).

    Routed through evidence_policy.resolve_grade so the precedence rule lives in
    one place: active task's task_mode -> derived grade, else legacy ledger grade,
    else STANDARD."""
    try:
        return resolve_grade(load_ledger(input_data), None)
    except Exception:
        return "STANDARD"


def _plan_mode_allows_stop(input_data: dict) -> bool:
    """Plan Mode: Stop must always allow so the user can review and approve the
    plan. The evidence gate (step 1) is INFINITE and its task checks routinely
    require repo mutation that plan mode forbids, so any block here traps the
    session in a no-exit loop instead of surfacing the plan -- the Codex symptom
    this guards. Detection: an explicit plan-mode flag on the Stop payload, else
    the shared PreToolUse resolver (transcript turn_context / ledger cache set at
    UserPromptSubmit). Fail-open to False: a detector bug never forces a stop, it
    falls through to the normal gates."""
    try:
        sc = input_data.get("session_context")
        if isinstance(sc, dict) and sc.get("plan_mode_enabled"):
            return True
        if input_data.get("plan_mode_enabled"):
            return True
    except Exception:
        pass
    try:
        from plan_mode import resolve_plan_mode_for_hooks

        return bool(resolve_plan_mode_for_hooks(input_data).get("enabled"))
    except Exception:
        return False


def main() -> int:
    input_data = read_stdin_json()

    try:
        from judge_transport import bind_session

        bind_session(input_data)
    except Exception:
        pass

    if not input_data.get("transcript_path"):
        resolved = locate_transcript(input_data)
        if resolved:
            input_data["transcript_path"] = resolved

    # Plan Mode short-circuit: always allow Stop so the user can approve the plan.
    # Resolved after transcript_path so the transcript turn_context / ledger scan
    # works. Must precede the INFINITE evidence gate (step 1) and the completion
    # handoff judge (step 2) -- plan mode forbids the repo mutation those checks
    # expect, so any block traps the session looping with no way out (seen on
    # Codex). Allow-stop emits {} per the AGENTS.md Stop contract.
    if _plan_mode_allows_stop(input_data):
        emit_json({})
        return 0
    from spec_io import canonical_project_root

    cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))
    grade = resolve_grade(load_ledger(input_data), os.environ.get("UNIFABLE_GRADE"))
    validate_ctx = ""
    validate_ctx_truncated = False
    stop_digest_path = ""

    # 1. Evidence gate — INFINITE. On a non-LIGHT task the evidence spec must EXIST
    #    and validate (repo_context {cite,why}, acceptance_criteria with live output,
    #    prior_art {cite,why}) before completion. This blocks EVERY stop — ignoring both
    #    the loop guard (stop_hook_active) and the stop-block cap — until a valid
    #    spec exists; the agent is unconditionally required to write its evidence
    #    back. The only releases: a valid spec, LIGHT grade, no resolvable session
    #    (fail open), the holdout 'off' arm, or a gate exception.
    if grade != "LIGHT":
        try:
            from spec_io import load_spec, resolve_session_id
            from spec_tasks import all_tasks_validated
            from spec_validation import validate_spec

            ledger = load_ledger(input_data)

            # Spec key = the session (one spec per directory+session). None ->
            # nothing resolvable -> fail open (skip the gate).
            task_key = resolve_session_id(input_data, default=None)
            spec = load_spec(cwd, task_key) if task_key else None
            val_msgs: list[str] = []
            ev_reason = ""
            loop_lift_ctx = ""
            if task_key and spec is None:
                ev_reason = (
                    "no evidence spec for this session (the prompt hook auto-creates one for "
                    "non-trivial work). Add a requirement with `unifable "
                    "add-task --title '<requirement>' --check '<runnable check>'`."
                )
            elif spec is not None:
                try:
                    from citations import (
                        activity_from_ledger,
                        enabled,
                        filter_gate_defect_citation_reasons,
                        merge_activity,
                        scan_transcript,
                        verify_citations,
                    )
                    from heavy_workflow import clear_stale_heavy_workflow, heavy_snapshot
                    from parse_tool_result import format_verifications
                    from spec_hygiene import apply_spec_hygiene
                    from spec_io import save_spec
                    from spec_stop_validate import auto_validate_spec

                    _stop_ledger = load_ledger(input_data)
                    ledger_activity = activity_from_ledger(_stop_ledger)
                    transcript_activity = scan_transcript(input_data.get("transcript_path"))
                    activity = merge_activity(ledger_activity, transcript_activity)
                    heavy_before = heavy_snapshot(spec)  # gap 2: phase/primary pre-state
                    stop_added: dict[str, list[str]] = {}
                    if apply_spec_hygiene(spec, activity, cwd, added_sink=stop_added)[0]:
                        save_spec(cwd, task_key, spec)
                    if clear_stale_heavy_workflow(spec, grade):
                        save_spec(cwd, task_key, spec)
                    stop_evidence = dict(activity)
                    stop_evidence["tool_evidence"] = [str(x) for x in (activity.get("tool_evidence") or [])][-60:]
                    stop_evidence["command_outputs"] = [str(x) for x in (activity.get("command_outputs") or [])][-60:]
                    stop_evidence["verifications"] = format_verifications(_stop_ledger.get("verification_results"))
                    spec, val_msgs = auto_validate_spec(
                        spec,
                        cwd,
                        time_budget=STOP_JUDGE_BUDGET,
                        transcript_path=input_data.get("transcript_path"),
                        evidence=stop_evidence,
                    )
                    save_spec(cwd, task_key, spec)
                    # Gap 2 + 7: enrich the Stop digest with HEAVY phase/primary
                    # transitions and sub-agent-credited citations. Both ride
                    # val_msgs, which only surfaces when the completion breaker
                    # blocks -- never on an allow-stop (AGENTS.md additionalContext rule).
                    extra_msgs = _stop_workflow_notes(
                        spec,
                        heavy_before,
                        stop_added,
                        ledger_activity,
                        transcript_activity,
                        cwd,
                    )
                    if extra_msgs:
                        val_msgs = list(val_msgs) + extra_msgs
                    validate_ctx, validate_ctx_truncated = _build_stop_validate_context(
                        spec,
                        val_msgs,
                    )
                    if validate_ctx:
                        full_ctx, _ = _build_stop_validate_context(
                            spec,
                            val_msgs,
                            max_len=1_000_000,
                        )
                        stop_digest_path = _persist_stop_digest(cwd, task_key, full_ctx)
                except Exception:
                    pass  # fail open
                # Breaker: a task-spec must have EVERY task validated (its check ran
                # AND the judge confirmed) before the breaker opens. Blocks every
                # stop until then.
                ok_tasks, incomplete = all_tasks_validated(spec)
                if not ok_tasks:
                    loop_lift_ctx = ""
                    try:
                        _led = load_ledger(input_data)
                        spec, ok_tasks, incomplete, val_msgs, validate_ctx, early = _handle_completion_loop_release(
                            input_data,
                            cwd,
                            task_key,
                            spec,
                            _led,
                            incomplete,
                            val_msgs,
                            validate_ctx,
                        )
                        if early is not None:
                            emit_json(early)
                            return 0
                        validate_ctx, validate_ctx_truncated = _build_stop_validate_context(
                            spec,
                            val_msgs,
                        )
                        if validate_ctx:
                            full_ctx, _ = _build_stop_validate_context(
                                spec,
                                val_msgs,
                                max_len=1_000_000,
                            )
                            stop_digest_path = _persist_stop_digest(cwd, task_key, full_ctx)
                        from loop_release import format_loop_lift_context, loop_lift_active

                        if loop_lift_active(_led):
                            loop_lift_ctx = format_loop_lift_context(_led)
                        else:
                            loop_lift_ctx = ""
                    except Exception:
                        pass  # fail open
                    if not ok_tasks:
                        # Host-agnostic safety cap (the circuit-breaker "bounded open
                        # state"): if the completion breaker has re-blocked Stop with no
                        # NET progress COMPLETION_MAX_STALLED_BLOCKS times, it is a
                        # runaway (judge adding requirements at least as fast as they
                        # validate). Release Stop with a loud escalation instead of
                        # trapping the session forever. Mirrors MAX_STOP_BLOCKS for the
                        # observation gate; fails open. Protects Codex/other hosts that
                        # lack a generic Stop-block cap.
                        try:
                            _led = load_ledger(input_data)
                            if note_completion_block(_led, len(incomplete)):
                                save_ledger(input_data, _led)
                                _warn = completion_runaway_warning(len(incomplete))
                                emit_json({"systemMessage": _warn})
                                return 0
                            save_ledger(input_data, _led)
                        except Exception:
                            pass  # fail open -- the safety cap never hard-blocks on its own bug
                        ev_reason = (
                            f"Completion gate blocked: {len(incomplete)} unresolved task(s) "
                            f"({', '.join(incomplete)})."
                        )
                        # Deterministic self-contradiction guard (Fix C): if an
                        # incomplete task's check requires an action the research-phase
                        # allowlist blocks, the gate can never be satisfied -- append a
                        # judge-independent notice with the allowed alternative so the
                        # agent escapes instead of looping on an async judge that may
                        # never fire. Fail-open (empty string on any error).
                        try:
                            from check_satisfiability import detect_self_contradiction

                            contradiction = detect_self_contradiction(spec, incomplete)
                            if contradiction:
                                ev_reason = f"{ev_reason}\n\n{contradiction}"
                        except Exception:
                            pass
                        if not str(validate_ctx or "").strip():
                            try:
                                from model_notify import format_blocking_task_hints, task_ids_from_headlines

                                ev_reason += format_blocking_task_hints(
                                    spec,
                                    incomplete,
                                    changed_ids=task_ids_from_headlines(val_msgs),
                                )
                            except Exception:
                                pass
                        if validate_ctx_truncated and stop_digest_path:
                            ev_reason += f"\nFull digest: {stop_digest_path}"
                        hint = _completion_stop_hint(input_data, spec, incomplete)
                        if hint:
                            ev_reason += "\n\n" + hint
                        # loop_lift_ctx is continuation guidance, not the short alarm:
                        # _emit_stop_payload -> finalize_stop_payload already routes it to
                        # additionalContext (Claude) / reason (Codex) exactly once. Appending
                        # it here too would double-emit the same lift block.
                else:
                    try:
                        _led = load_ledger(input_data)
                        reset_completion_stall(_led)
                        save_ledger(input_data, _led)
                    except Exception:
                        pass  # fail open
                    ok, reasons = validate_spec(
                        spec,
                        grade,
                        require_evidence=True,
                        evidence_profile=resolve_evidence_profile(ledger, spec),
                    )
                    if not ok:
                        from spec_contracts import format_spec_validation_block

                        ev_reason = format_spec_validation_block(
                            grade,
                            reasons,
                            resolve_evidence_profile(ledger, spec),
                            spec,
                        )
                    else:
                        # Citation truth-check: code-profile tasks only.
                        try:
                            from citations import (
                                activity_from_ledger,
                                enabled,
                                filter_gate_defect_citation_reasons,
                                format_citation_verify_message,
                                merge_activity,
                                scan_transcript,
                                verify_citations,
                            )

                            profile = resolve_evidence_profile(ledger, spec)
                            if enabled() and profile != "operational":
                                activity = merge_activity(
                                    activity_from_ledger(load_ledger(input_data)),
                                    scan_transcript(input_data.get("transcript_path")),
                                )
                                cited = verify_citations(spec, activity, cwd, require_commands=True)
                                cited = filter_gate_defect_citation_reasons(spec, cited, cwd)
                                if cited:
                                    ev_reason = format_citation_verify_message(cited)
                        except Exception:
                            pass  # fail open
            if ev_reason:
                # M3 holdout (env-gated, default off): 'off' arm skips the gate so
                # the gate's effect can be measured against a pure baseline.
                if _holdout_suppresses(input_data):
                    _log_holdout(input_data, ev_reason)
                    emit_json({})
                    return 0
                payload = {
                    "decision": "block",
                    "reason": ev_reason,
                }
                _emit_stop_payload(
                    payload,
                    input_data,
                    validate_ctx=validate_ctx,
                    loop_lift_ctx=loop_lift_ctx,
                    digest_path=stop_digest_path,
                )
                return 0
        except Exception:
            pass  # fail open — a gate bug never interrupts the host

    # 2. Completion handoff judge — blocks text-only turns that defer autonomous
    #    work. Bypasses stop_hook_active; capped in completion_handoff.py.
    try:
        from completion_handoff import completion_handoff_decision

        handoff_payload = completion_handoff_decision(input_data, cwd)
        if handoff_payload:
            steering = str(handoff_payload.pop("_handoff_steering", "") or "").strip()
            # The evidence-spec board (validate_ctx, e.g. "Spec complete: all tasks
            # validated.") is a statement ABOUT the evidence gate; stapling it onto a
            # handoff block produces a self-contradictory "blocked + all validated"
            # message. The board rides ONLY the step-1 evidence-gate block; the digest
            # file pointer is fine to keep.
            _emit_stop_payload(
                handoff_payload,
                input_data,
                validate_ctx="",
                loop_lift_ctx=steering,
                digest_path=stop_digest_path,
            )
            return 0
    except Exception:
        pass  # fail open

    # 3. Loop guard for the softer gates below (findings + observation): never
    #    block twice in a row on the same stop.
    if input_data.get("stop_hook_active") is True:
        emit_json({})
        return 0

    # 4. Findings cross-link (opt-in): open high/critical findings block
    #    completion. Fails open.
    try:
        from findings import blocking_findings

        blockers = blocking_findings(cwd)
    except Exception:
        blockers = []
    if blockers:
        ids = ", ".join(str(f.get("id", "?")) for f in blockers)
        emit_json(
            {
                "decision": "block",
                "reason": f"{len(blockers)} open high/critical finding(s) to resolve or reject before completing: {ids}.",
            }
        )
        return 0

    # 5. Observation gate — should_block_stop (deep changed-but-unverified). This
    #    softer nudge keeps the MAX_STOP_BLOCKS cap + holdout, so it never traps.
    ledger = load_ledger(input_data)
    block, obs_reason = should_block_stop(ledger, grade)
    if block:
        if _holdout_suppresses(input_data):
            _log_holdout(input_data, obs_reason)
            emit_json({})
            return 0
        if int(ledger.get("stop_blocks") or 0) < MAX_STOP_BLOCKS:
            ledger["stop_blocks"] = int(ledger.get("stop_blocks") or 0) + 1
            save_ledger(input_data, ledger)
            emit_json({"decision": "block", "reason": obs_reason})
            return 0

    warning = warning_after_max_blocks(ledger)
    # _advance_release and _advance_auto_verify must still run (they persist
    # breaker state: drain the release lane, run the release judge, poll
    # auto-verify, disarm). But their returned notes are internal breaker state
    # the model cannot act on at end of turn, so they are NOT surfaced in the
    # model-facing systemMessage. Only the genuine missing-verification nudge
    # (warning_after_max_blocks) reaches the model on the allow-stop path.
    _advance_release(input_data)
    _advance_auto_verify(input_data)
    if warning and str(warning).strip():
        emit_json({"systemMessage": str(warning).strip()})
    else:
        emit_json({})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        emit_json({"systemMessage": f"Stop hook failed open: {exc}"})
        raise SystemExit(0)
