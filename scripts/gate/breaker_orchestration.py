#!/usr/bin/env python3
"""Groundedness-breaker orchestration: the PreToolUse / PostToolUse evaluate
entrypoints that wire the judges to persisted breaker state under a lock.

Extracted from groundedness.py; re-exported by the groundedness facade. Top of
the DAG: imports judges + runtime + state primitives.
"""
from __future__ import annotations

from typing import Any

try:
    from breaker_judges import (
        JudgeFn,
        arm_judge,
        disarm_judge,
        monitor_provisional_judge,
    )
    from breaker_prompts import _SCOPE_HINT_PREFIX
    from breaker_runtime import (
        _apply_release,
        _disarm_message,
        _fail_open_message,
        _needed_message,
        _release_log,
        _stale_arm_message,
        _user_goal_block,
        _verify_confirmed_message,
        _verify_disarm_digest,
        _verify_dispatched_message,
        _verify_failed_message,
        auto_verify_in_progress,
        breaker_key,
        clear_auto_verify,
        directives_near_duplicate,
        disarm,
        is_mutation_tool,
        is_release_tool,
        judge_transcript,
        max_blocks,
        record_verdict,
        resolve_task_lineage,
        should_coalesce,
        should_judge,
    )
    from breaker_state import (
        append_event,
        breaker_lock,
        claim_already_adjudicated,
        load_breaker,
        record_adjudicated_claim,
        reinstate,
        save_breaker,
    )
except ImportError:  # pragma: no cover
    from scripts.gate.breaker_judges import (
        JudgeFn,
        arm_judge,
        disarm_judge,
        monitor_provisional_judge,
    )
    from scripts.gate.breaker_prompts import _SCOPE_HINT_PREFIX
    from scripts.gate.breaker_runtime import (
        _apply_release,
        _disarm_message,
        _fail_open_message,
        _needed_message,
        _release_log,
        _stale_arm_message,
        _user_goal_block,
        _verify_confirmed_message,
        _verify_disarm_digest,
        _verify_dispatched_message,
        _verify_failed_message,
        auto_verify_in_progress,
        breaker_key,
        clear_auto_verify,
        directives_near_duplicate,
        disarm,
        is_mutation_tool,
        is_release_tool,
        judge_transcript,
        max_blocks,
        record_verdict,
        resolve_task_lineage,
        should_coalesce,
        should_judge,
    )
    from scripts.gate.breaker_state import (
        append_event,
        breaker_lock,
        claim_already_adjudicated,
        load_breaker,
        record_adjudicated_claim,
        reinstate,
        save_breaker,
    )


def _dispatch_auto_verify(
    input_data: dict,
    state: dict,
    claim: str,
    tasks: list[dict[str, Any]],
    now: float,
) -> None:
    """Spawn the background verification runner for sanctioned tasks and record the
    auto-verify state. Fail-open: any error leaves the arm as a normal block."""
    try:
        try:
            from verify_lane import dispatch_verification
        except ImportError:  # pragma: no cover
            from scripts.gate.verify_lane import dispatch_verification

        cwd = str((input_data or {}).get("cwd") or "")
        key = dispatch_verification(input_data, claim, tasks, cwd)
        if not key:
            return
        state["breaker_verify_key"] = key
        state["breaker_verify_tasks"] = [
            {
                "subclaim": str(t.get("subclaim") or ""),
                "command": str(t.get("command") or ""),
                "status": "pending",
                "exit": None,
                "tail": "",
            }
            for t in tasks
            if isinstance(t, dict) and str(t.get("command") or "").strip()
        ]
        state["breaker_verify_dispatched_at"] = now
        commands = [str(t.get("command") or "") for t in tasks if isinstance(t, dict)]
        append_event(state, "VERIFY_DISPATCH", claim=claim, command="; ".join(commands))
        state["breaker_steering"] = _verify_dispatched_message(commands)
    except Exception:
        return


def _poll_auto_verify(input_data: dict, state: dict, now: float) -> str:
    """Advance a dispatched background verification from its sidecar.

    Reads per-command results; for each newly-finished task feeds the deterministic
    exit code (the policy-sanctioned command IS the falsifiable check) into the
    auto-verify decision: exit 0 confirms the subclaim, non-zero fails it. When ALL
    subclaims confirm, the breaker disarms with an aggregate digest. When every task
    is terminal but not all passed, auto-verify state is cleared so grounding reverts
    to the normal transcript path (the model fixes + proves it). Fail-open to ''."""
    try:
        try:
            from verify_lane import read_verification_results
        except ImportError:  # pragma: no cover
            from scripts.gate.verify_lane import read_verification_results

        key = str(state.get("breaker_verify_key") or "")
        tasks = state.get("breaker_verify_tasks")
        if not key or not isinstance(tasks, list) or not tasks:
            return ""
        results = read_verification_results(input_data, key)
        msgs: list[str] = []
        for t in tasks:
            if not isinstance(t, dict) or str(t.get("status") or "") in ("passed", "failed"):
                continue
            r = results.get(str(t.get("command") or ""))
            if not isinstance(r, dict):
                continue
            try:
                ec = int(r.get("exit"))
            except (TypeError, ValueError):
                continue
            t["exit"] = ec
            t["tail"] = str(r.get("tail") or "")
            sub = str(t.get("subclaim") or "")
            cmd = str(t.get("command") or "")
            if ec == 0:
                t["status"] = "passed"
                append_event(state, "VERIFY_RESULT", subclaim=sub, command=cmd, exit=ec)
                msgs.append(_verify_confirmed_message(sub, cmd, ec))
            else:
                t["status"] = "failed"
                append_event(state, "VERIFY_RESULT", subclaim=sub, command=cmd, exit=ec)
                msgs.append(_verify_failed_message(cmd, ec, t.get("tail") or ""))
        live = [t for t in tasks if isinstance(t, dict)]
        if live and all(str(t.get("status") or "") == "passed" for t in live):
            claim = str(state.get("breaker_claim") or "")
            confirmations = [(str(t.get("subclaim") or ""), str(t.get("command") or ""), t.get("exit")) for t in live]
            append_event(state, "DISARM", claim=claim, grounded=True)
            record_adjudicated_claim(state, claim)
            disarm(state)  # also clears auto-verify state
            return _verify_disarm_digest(confirmations)
        if live and all(str(t.get("status") or "") in ("passed", "failed") for t in live):
            # Finished with at least one failure: hand grounding back to the model
            # via the normal transcript disarm path.
            clear_auto_verify(state)
        return "\n".join(msgs) if msgs else ""
    except Exception:
        return ""


def evaluate_pre_tool(
    input_data: dict,
    state: dict,
    now: float,
    active_task: str,
    judge: JudgeFn | None = None,
    coalesce: bool = False,
) -> tuple[bool, str, str]:
    """PreToolUse path: arm judge (debounced) and block mutation tools while armed.

    When `coalesce` is True the imminent call belongs to a parallel batch that a
    sibling process already judged: every judge call is skipped and the block
    decision falls through to the already-persisted breaker state, so the batch
    costs one judge call, not N. The locked wrapper (evaluate_pre_tool_locked) is
    the only caller that sets it; direct callers keep the original behavior."""
    tool = str(input_data.get("tool_name") or "")
    key = breaker_key(str(input_data.get("session_id") or ""), resolve_task_lineage(input_data, active_task))
    events = state.get("events") if isinstance(state.get("events"), list) else []
    notify_out = ""
    stale_notify = ""
    try:
        armed = bool(state.get("breaker_armed"))
        provisional = bool(state.get("breaker_provisional"))
        if (armed or provisional) and state.get("breaker_key") != key:
            stale_claim = str(state.get("breaker_claim") or "")
            append_event(state, "STALE_ARM_DROPPED", claim=stale_claim)
            disarm(state)
            armed = False
            provisional = False
            stale_notify = _stale_arm_message(stale_claim)
        if provisional:
            claim = str(state.get("breaker_claim") or "")
            user_goal = _user_goal_block(input_data, active_task)
            segment = judge_transcript(input_data, events)
            if claim and segment.strip() and not coalesce:
                state["breaker_judge_call_at"] = now
                release_verdict = disarm_judge(claim, segment, user_goal=user_goal, judge=judge)
                disarmed, lift_msg = _apply_release(state, claim, release_verdict)
                if disarmed:
                    provisional = False
                    state["breaker_pending_notify"] = _disarm_message()
                elif lift_msg:
                    state["breaker_pending_notify"] = lift_msg
            if state.get("breaker_provisional") and is_mutation_tool(tool):
                scope = str(state.get("breaker_lift_scope") or "")
                if claim and scope and not coalesce:
                    state["breaker_judge_call_at"] = now
                    drift, feedback = monitor_provisional_judge(
                        claim,
                        scope,
                        segment,
                        tool,
                        user_goal=user_goal,
                        judge=judge,
                    )
                    if drift == 2:
                        append_event(state, "REINSTATE", claim=claim, corrective=feedback)
                        reinstate(state, claim, feedback or "Return to the verification scope.")
                        return True, feedback or "Return to the verification scope.", ""
                    if drift == 1 and feedback:
                        append_event(state, "SCOPE_HINT", claim=claim, hint=feedback)
                        hint_msg = f"{_SCOPE_HINT_PREFIX}{feedback}"
                        existing = str(state.get("breaker_pending_notify") or "")
                        state["breaker_pending_notify"] = f"{existing}\n{hint_msg}".strip() if existing else hint_msg
            pending = str(state.get("breaker_pending_notify") or "")
            if pending:
                state["breaker_pending_notify"] = ""
                notify_out = pending
            return False, "", notify_out
        if not armed and not coalesce and should_judge(state, key, now):
            segment = judge_transcript(input_data, events)
            state["breaker_judge_call_at"] = now
            director_out: dict[str, Any] = {}
            verdict, steering, claim = arm_judge(
                segment, events=events, judge=judge, input_data=input_data, out=director_out
            )
            if verdict == 1 and claim and claim_already_adjudicated(
                claim, events, extra_claims=state.get("breaker_adjudicated_claims")
            ):
                verdict, steering, claim = 0, "", ""
            record_verdict(state, key, now, verdict, steering, claim)
            # Stepwise director: persist directive + scope from this (debounced)
            # call. When arming, the breaker owns the block, so clear the scope and
            # let steering carry the instruction. When disarmed, enforce the scope
            # and surface the directive on the allow path (~once per debounce window).
            if verdict == 1:
                state["breaker_directive"] = ""
                state["breaker_tool_scope"] = {}
                state["breaker_last_directive_surfaced"] = ""
                # Auto-grounding: if the judge decomposed the claim into sanctioned
                # verification tasks, dispatch them to the background runner now. The
                # arm still blocks the first mutation; later calls poll for results.
                vtasks = director_out.get("verify_tasks")
                if isinstance(vtasks, list) and vtasks and claim and not coalesce:
                    _dispatch_auto_verify(input_data, state, claim, vtasks, now)
            else:
                directive = str(director_out.get("directive") or "")
                scope = director_out.get("tool_scope")
                state["breaker_directive"] = directive
                state["breaker_tool_scope"] = scope if isinstance(scope, dict) else {}
                # Surface a directive only when it is genuinely NEW work. Byte-exact
                # equality misses the real re-request failure mode -- the judge
                # re-WORDING an already-satisfied step every debounce window -- so a
                # near-duplicate of the last surfaced directive stays silent too.
                if directive and not directives_near_duplicate(
                    directive, str(state.get("breaker_last_directive_surfaced") or "")
                ):
                    msg = directive
                    existing = str(state.get("breaker_pending_notify") or "")
                    state["breaker_pending_notify"] = f"{existing}\n{msg}".strip() if existing else msg
                    state["breaker_last_directive_surfaced"] = directive
        elif armed:
            # Auto-grounding owns the disarm decision while a background verification
            # is dispatched: poll the sidecar instead of judging the transcript. Once
            # it finishes (all passed -> disarm; any failure -> cleared, falls back
            # below on the next call), normal transcript grounding resumes.
            if state.get("breaker_verify_key"):
                notify = _poll_auto_verify(input_data, state, now)
                if notify:
                    state["breaker_pending_notify"] = notify
            else:
                claim = str(state.get("breaker_claim") or "")
                if claim and not coalesce:
                    state["breaker_judge_call_at"] = now
                    segment = judge_transcript(input_data, events)
                    user_goal = _user_goal_block(input_data, active_task)
                    release_verdict = disarm_judge(claim, segment, user_goal=user_goal, judge=judge)
                    disarmed, lift_msg = _apply_release(state, claim, release_verdict)
                    if disarmed:
                        state["breaker_pending_notify"] = _disarm_message()
                    elif lift_msg:
                        state["breaker_pending_notify"] = lift_msg
                    elif release_verdict.needed:
                        state["breaker_pending_notify"] = _needed_message(release_verdict.needed)
    except Exception:
        return False, "", ""
    if is_mutation_tool(tool) and state.get("breaker_armed"):
        # While a dispatched background verification is still running (inside its
        # window), exempt the block from the fail-open cap -- the breaker is grounding
        # the claim itself; a slow suite must not trip BREAKER_MAX_BLOCKS. The verify
        # timeout bounds this instead. Surface the latest dispatch/confirmation notice
        # as the block message so progress is visible while still blocked.
        verifying = auto_verify_in_progress(state, now)
        if not coalesce and not verifying:
            count = int(state.get("breaker_block_count") or 0) + 1
            state["breaker_block_count"] = count
            if count >= max_blocks():
                _release_log(count)
                claim = str(state.get("breaker_claim") or "")
                append_event(state, "FAIL_OPEN", claim=claim, block_count=count)
                record_adjudicated_claim(state, claim)
                disarm(state)
                return False, "", _fail_open_message(count, claim)
        if verifying:
            pending = str(state.get("breaker_pending_notify") or "")
            state["breaker_pending_notify"] = ""
            return True, (pending or str(state.get("breaker_steering") or "")), ""
        state["breaker_pending_notify"] = ""
        return True, str(state.get("breaker_steering") or ""), ""
    pending = str(state.get("breaker_pending_notify") or "")
    if pending:
        state["breaker_pending_notify"] = ""
        notify_out = pending
    if stale_notify:
        notify_out = f"{stale_notify}\n{notify_out}".strip() if notify_out else stale_notify
    return False, "", notify_out


def evaluate_pre_tool_locked(
    input_data: dict,
    now: float,
    active_task: str,
    judge: JudgeFn | None = None,
    timeout: float | None = None,
) -> tuple[bool, str, str, dict]:
    """Load -> evaluate -> save the breaker under a cross-process lock.

    This is the hook entrypoint. The lock serializes the concurrent PreToolUse
    processes of a parallel tool-call batch: the first to acquire it judges and
    persists its verdict; the rest then load that fresh state, see a judge already
    fired within the coalesce window, and skip their own (identical) judge call.
    Result: one judge API call per batch instead of N, with blocking semantics
    unchanged. Fail-open: breaker_lock runs the body unlocked if it cannot lock,
    and any error leaves the tool unblocked. Returns the state too so the caller
    can inspect the event log (e.g. for REINSTATE)."""
    with breaker_lock(input_data, timeout):
        state = load_breaker(input_data)
        key = breaker_key(str(input_data.get("session_id") or ""), resolve_task_lineage(input_data, active_task))
        coalesce = should_coalesce(state, key, now)
        block, steering, notify = evaluate_pre_tool(input_data, state, now, active_task, judge=judge, coalesce=coalesce)
        save_breaker(input_data, state)
    return block, steering, notify, state


def evaluate_post_tool_release(
    input_data: dict,
    state: dict,
    fresh_tool: str,
    active_task: str = "",
    judge: JudgeFn | None = None,
) -> tuple[bool, str, str]:
    """PostToolUse release path. Returns (fully_disarmed, needed, context_message)."""
    armed = bool(state.get("breaker_armed"))
    provisional = bool(state.get("breaker_provisional"))
    if not armed and not provisional:
        return False, "", ""
    # Auto-grounding poll runs first and on ANY PostToolUse (it reads the sidecar, not
    # the tool): a background verification can disarm at end of a read step even when
    # the model never re-ran anything. While it owns grounding, skip transcript disarm.
    if state.get("breaker_verify_key"):
        import time as _time

        notify = _poll_auto_verify(input_data, state, _time.time())
        if not state.get("breaker_armed"):
            return True, "", notify or _disarm_message()
        if state.get("breaker_verify_key"):
            return False, "", notify
    tool = str(input_data.get("tool_name") or "")
    if not is_release_tool(tool, input_data):
        return False, "", ""
    claim = str(state.get("breaker_claim") or "")
    if not claim:
        return False, "", ""
    events = state.get("events") if isinstance(state.get("events"), list) else []
    try:
        segment = judge_transcript(input_data, events, fresh_tool=fresh_tool)
        user_goal = _user_goal_block(input_data, active_task)
        release_verdict = disarm_judge(claim, segment, user_goal=user_goal, judge=judge)
        disarmed, lift_msg = _apply_release(state, claim, release_verdict)
        if disarmed:
            return True, "", _disarm_message()
        if lift_msg:
            return False, "", lift_msg
        if release_verdict.needed:
            return False, release_verdict.needed, _needed_message(release_verdict.needed)
    except Exception:
        return False, "", ""
    return False, "", ""


evaluate = evaluate_pre_tool
