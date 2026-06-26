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
        breaker_key,
        directives_near_duplicate,
        disarm,
        is_mutation_tool,
        is_release_tool,
        judge_transcript,
        max_blocks,
        record_verdict,
        should_coalesce,
        should_judge,
    )
    from breaker_state import (
        append_event,
        breaker_lock,
        claim_already_adjudicated,
        load_breaker,
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
        breaker_key,
        directives_near_duplicate,
        disarm,
        is_mutation_tool,
        is_release_tool,
        judge_transcript,
        max_blocks,
        record_verdict,
        should_coalesce,
        should_judge,
    )
    from scripts.gate.breaker_state import (
        append_event,
        breaker_lock,
        claim_already_adjudicated,
        load_breaker,
        reinstate,
        save_breaker,
    )


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
    key = breaker_key(str(input_data.get("session_id") or ""), str(active_task or ""))
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
            if verdict == 1 and claim and claim_already_adjudicated(claim, events):
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
        if not coalesce:
            count = int(state.get("breaker_block_count") or 0) + 1
            state["breaker_block_count"] = count
            if count >= max_blocks():
                _release_log(count)
                claim = str(state.get("breaker_claim") or "")
                append_event(state, "FAIL_OPEN", claim=claim, block_count=count)
                disarm(state)
                return False, "", _fail_open_message(count, claim)
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
        key = breaker_key(str(input_data.get("session_id") or ""), str(active_task or ""))
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
