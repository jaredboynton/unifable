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
  2. Promise-no-act guard: if the last assistant text only promises future work
     without a tool call or user question, block once and force the work to happen.
  3. Observation gate: a non-quick, non-docs task that changed files but has no
     observed successful verification. Softer — capped at MAX_STOP_BLOCKS then
     advisory-only, behind the stop_hook_active loop guard, so it never traps.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "gate"))
sys.path.insert(0, str(_HERE.parent / "scripts" / "shadow"))

from atomicio import write_text_atomic
from evidence_policy import resolve_evidence_profile, resolve_grade
from ledger import emit_json, load_ledger, read_stdin_json, save_ledger
from transcript_tail import TRANSCRIPT_TOKEN_BUDGET, stripped_transcript_tail
from transcript_locate import locate_transcript
from verify_state import (MAX_STOP_BLOCKS, completion_runaway_warning,
                          note_completion_block, reset_completion_stall,
                          should_block_stop, warning_after_max_blocks)

GOAL_TRANSCRIPT_TOKENS = TRANSCRIPT_TOKEN_BUDGET
try:
    GOAL_STOP_BLOCK_CAP = int(os.environ.get("UNIFABLE_GOAL_STOP_BLOCK_CAP", "8"))
except ValueError:
    GOAL_STOP_BLOCK_CAP = 8
# Wall-clock budget for the judge/check work this Stop hook does, kept under the
# host Stop-hook timeout (hooks.json wires gate_stop at 120s). auto_validate_spec
# stops launching work past this deadline so the hook always returns cleanly
# instead of being killed mid-judge (the codex-thread 10s timeout).
try:
    STOP_JUDGE_BUDGET = float(os.environ.get("UNIFABLE_STOP_BUDGET", "100"))
except ValueError:
    STOP_JUDGE_BUDGET = 100.0
GOAL_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "impossible": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["ok", "reason"],
    "additionalProperties": False,
}

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
        if count < COMPLETION_HINT_THRESHOLD or \
                (count - COMPLETION_HINT_THRESHOLD) % COMPLETION_HINT_STEP != 0:
            return ""
        from spec import judge_hint

        recent = " | ".join(
            (ledger.get("ran_commands") or [])[-6:]
            + [f"failure:{f}" for f in (ledger.get("failures") or [])[-3:]]
        )
        signal = (
            f"The completion breaker has re-blocked Stop {count} times; "
            f"{len(incomplete)} requirement(s) still not validated "
            f"({', '.join(incomplete)}). The agent may be looping on "
            "The agent may be looping without converging."
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


def _emit_stop_payload(
    payload: dict,
    input_data: dict,
    *,
    validate_ctx: str = "",
    loop_lift_ctx: str = "",
    digest_path: str = "",
) -> None:
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
        from spec import session_dir

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
    from spec import all_tasks_validated, save_spec

    update_loop_signature(ledger, incomplete)

    if loop_lift_active(ledger) and consume_provisional_stop_lift(ledger):
        msg = provisional_allow_message(ledger)
        save_ledger(input_data, ledger)
        payload = {"systemMessage": msg}
        return spec, False, incomplete, val_msgs, validate_ctx, payload

    if should_invoke_loop_judge(ledger, incomplete, pending_block=True, spec=spec):
        recent = " | ".join(
            (ledger.get("ran_commands") or [])[-6:]
            + [f"failure:{f}" for f in (ledger.get("failures") or [])[-3:]]
        )
        signal = (
            f"Completion breaker blocked Stop with {len(incomplete)} incomplete task(s) "
            f"({', '.join(incomplete)}). stall_blocks={ledger.get('completion_stall_blocks')} "
            f"stop_blocks={ledger.get('completion_stop_blocks')} "
            f"same_set_streak={ledger.get('loop_same_set_streak')}."
        )
        verdict = judge_completion_loop_release(spec, ledger, signal=signal, recent=recent)
        headlines, _lift_msg = apply_loop_release_verdict(spec, ledger, verdict)
        if headlines:
            val_msgs = list(val_msgs) + headlines
            validate_ctx, _ = _build_stop_validate_context(spec, val_msgs)
        save_spec(cwd, task_key, spec)
        save_ledger(input_data, ledger)
        ok_tasks, incomplete = all_tasks_validated(spec)
        return spec, ok_tasks, incomplete, val_msgs, validate_ctx, None

    save_ledger(input_data, ledger)
    ok_tasks, _ = all_tasks_validated(spec)
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
        append_event(make_event(input_data.get("session_id") or "", "holdout_suppress",
                                {"would_block_reason": reason}))
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


def _last_assistant_text_and_tool(transcript_path: str | None) -> tuple[str, bool]:
    if not transcript_path:
        return "", False
    path = Path(transcript_path)
    if not path.is_file():
        return "", False

    last_text = ""
    last_had_tool = False
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message", obj)
            if obj.get("type") != "assistant" and msg.get("role") != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            tools = [
                block for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            if texts or tools:
                last_text = "\n".join(texts).strip()
                last_had_tool = bool(tools)
    return last_text, last_had_tool


def _transcript_for_goal_judge(transcript_path: str | None, input_data: dict) -> str:
    text = stripped_transcript_tail(transcript_path, GOAL_TRANSCRIPT_TOKENS)
    if text.strip():
        return text
    if input_data.get("last_assistant_message"):
        return f"assistant: {input_data.get('last_assistant_message')}"
    return "(no transcript available)"


def _goals_path(cwd: str | Path, session_id: str | None) -> Path:
    # The goals plan lives beside the spec in the per-(directory, session) dir, so a
    # new session never inherits a prior session's plan (the stale-plan bleed fix).
    from spec import session_dir
    return session_dir(cwd, session_id) / "goals.json"


def _load_goal_plan(cwd: str | Path, session_id: str | None) -> dict | None:
    path = _goals_path(cwd, session_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _current_goal(plan: dict) -> dict | None:
    goals = plan.get("goals")
    if not isinstance(goals, list):
        return None
    for goal in goals:
        if isinstance(goal, dict) and goal.get("status") == "in_progress":
            return goal
    for goal in goals:
        if isinstance(goal, dict) and goal.get("status") == "pending":
            return goal
    return None


def _remaining_goals(plan: dict) -> list[dict]:
    goals = plan.get("goals")
    if not isinstance(goals, list):
        return []
    return [
        goal for goal in goals
        if isinstance(goal, dict) and goal.get("status") in {"pending", "in_progress"}
    ]


def _goal_condition(plan: dict, goal: dict) -> str:
    brief = str(plan.get("brief") or "").strip()
    title = str(goal.get("title") or "").strip()
    objective = str(goal.get("objective") or "").strip()
    gid = str(goal.get("id") or "?").strip()
    return (
        f"Plan brief: {brief or '(none)'}\n"
        f"Current goal {gid} {title}: {objective}\n"
        "The condition is satisfied only if the transcript contains clear evidence "
        "that this current goal is complete."
    )


def _goal_hook_arguments(input_data: dict) -> dict:
    keys = (
        "session_id", "transcript_path", "cwd", "permission_mode", "effort",
        "hook_event_name", "stop_hook_active", "last_assistant_message",
        "background_tasks", "session_crons",
    )
    return {key: input_data.get(key) for key in keys if key in input_data}


def _judge_goal_condition(condition: str, transcript: str, input_data: dict) -> dict:
    from codex_judge import ask_structured
    from transcript_tail import fit_judge_user_message

    system = (
        "You are evaluating a stop-condition hook in unifable. Read the conversation "
        "transcript carefully, then judge whether the user-provided condition is satisfied. "
        "Your response must be a JSON object with one of these shapes:\n"
        '- {"ok": true, "reason": "<quote evidence from the transcript that satisfies the condition>"}\n'
        '- {"ok": false, "reason": "<quote what is missing or what blocks the condition>"}\n'
        '- {"ok": false, "impossible": true, "reason": "<explain why the condition can never be satisfied>"}\n'
        'Always include a "reason" field, quoting specific text from the transcript whenever possible. '
        'If the transcript does not contain clear evidence that the condition is satisfied, return '
        '{"ok": false, "reason": "insufficient evidence in transcript"}. Only use '
        '{"ok": false, "impossible": true} when the condition is genuinely unachievable in this session.'
    )
    args = json.dumps(_goal_hook_arguments(input_data), ensure_ascii=False, sort_keys=True)
    user = fit_judge_user_message(
        "Conversation transcript:\n",
        transcript,
        suffix=(
            "\n\nBased on the conversation transcript above, has the following stopping condition been "
            "satisfied? Answer based on transcript evidence only.\n"
            f"Condition: {condition}\n\n"
            f"ARGUMENTS: {args}"
        ),
    )
    return ask_structured(system, user, GOAL_JUDGE_SCHEMA, schema_name="goal_stop")


def _mark_goal(cwd: str | Path, session_id: str | None, plan: dict, goal_id: str, status: str, reason: str) -> None:
    changed = False
    for goal in plan.get("goals", []):
        if isinstance(goal, dict) and str(goal.get("id")) == goal_id:
            goal["status"] = status
            goal["evidence"] = reason
            goal["stop_judge_reason"] = reason
            changed = True
            break
    if changed:
        write_text_atomic(_goals_path(cwd, session_id), json.dumps(plan, ensure_ascii=False, indent=1))


def goal_stop_decision(input_data: dict, cwd: str) -> dict | None:
    """Evaluate the active goals.py goal like Cursor's prompt Stop hook, using
    gpt-realtime-2 through codex_judge. Returns a Stop payload or None to allow."""
    if not input_data or input_data.get("_parse_error"):
        return None
    if not (
        input_data.get("session_id")
        or input_data.get("transcript_path")
        or input_data.get("last_assistant_message")
    ):
        return None
    from spec import resolve_session_id
    session_id = resolve_session_id(input_data, default=None)
    plan = _load_goal_plan(cwd, session_id)
    if not plan:
        return None
    goal = _current_goal(plan)
    if not goal:
        return None

    ledger = load_ledger(input_data)
    if int(ledger.get("goal_stop_blocks") or 0) >= GOAL_STOP_BLOCK_CAP:
        return {
            "systemMessage": (
                "unifable goal stop hook block cap reached; allowing stop with "
                "an incomplete goals.py plan."
            )
        }

    condition = _goal_condition(plan, goal)
    transcript = _transcript_for_goal_judge(input_data.get("transcript_path"), input_data)
    gid = str(goal.get("id") or "?")
    try:
        verdict = _judge_goal_condition(condition, transcript, input_data)
    except Exception as exc:  # noqa: BLE001
        verdict = {"ok": False, "reason": f"goal judge unavailable: {exc}"}

    reason = str(verdict.get("reason") or "insufficient evidence in transcript")
    if verdict.get("ok") is True:
        _mark_goal(cwd, session_id, plan, gid, "complete", reason)
        ledger["goal_stop_blocks"] = 0
        save_ledger(input_data, ledger)
        if _remaining_goals(plan):
            return {
                "decision": "block",
                "reason": (
                    f"Stop hook feedback:\n[{gid}] {reason}\n"
                    "The current goal is complete, but the goals.py plan still has "
                    "remaining goals. Continue with `python3 scripts/goals.py next`."
                ),
            }
        return None

    if verdict.get("impossible") is True:
        _mark_goal(cwd, session_id, plan, gid, "failed", reason)
        ledger["goal_stop_blocks"] = 0
        save_ledger(input_data, ledger)
        return {"systemMessage": f"unifable goal failed and was cleared from Stop blocking: [{gid}] {reason}"}

    ledger["goal_stop_blocks"] = int(ledger.get("goal_stop_blocks") or 0) + 1
    save_ledger(input_data, ledger)
    return {"decision": "block", "reason": f"Stop hook feedback:\n[{gid}] {reason}"}


def promise_no_act_reason(input_data: dict) -> str:
    """Return a Stop-block reason when the last assistant turn promised work
    instead of doing it. Fails open on transcript problems."""
    try:
        last_text, last_had_tool = _last_assistant_text_and_tool(input_data.get("transcript_path"))
    except Exception:
        return ""
    if last_had_tool or not last_text:
        return ""

    tail = last_text[-400:]
    promise = re.search(
        r"\b(I'?ll|I will|let me|next,? I|now I'?ll)\b[^.]{0,60}"
        r"\b(now|next|then|implement|create|write|add|run|fix|save|build|start|proceed)\b",
        tail,
        re.IGNORECASE,
    )
    asks_user = re.search(
        r"(\?|shall i|would you like|do you want|let me know|which option)",
        tail,
        re.IGNORECASE,
    )
    if promise and not asks_user:
        return (
            "Your previous response ended by stating an intent to do work without actually doing it. "
            "Do that work now with tool calls. End the turn only when the task is complete or you are "
            "blocked on input that only the user can provide."
        )
    return ""


def main() -> int:
    input_data = read_stdin_json()
    if not input_data.get("transcript_path"):
        resolved = locate_transcript(input_data)
        if resolved:
            input_data["transcript_path"] = resolved
    from spec import canonical_project_root

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
            from spec import all_tasks_validated, load_spec, resolve_session_id, validate_spec

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
                    from citations import (activity_from_ledger, enabled,
                                           filter_gate_defect_citation_reasons,
                                           merge_activity, scan_transcript,
                                           verify_citations)
                    from heavy_workflow import (
                                                clear_stale_heavy_workflow,
                                                heavy_snapshot)
                    from spec import auto_validate_spec, save_spec
                    from spec_hygiene import apply_spec_hygiene

                    ledger_activity = activity_from_ledger(load_ledger(input_data))
                    transcript_activity = scan_transcript(input_data.get("transcript_path"))
                    activity = merge_activity(ledger_activity, transcript_activity)
                    heavy_before = heavy_snapshot(spec)  # gap 2: phase/primary pre-state
                    stop_added: dict[str, list[str]] = {}
                    if apply_spec_hygiene(spec, activity, cwd, added_sink=stop_added)[0]:
                        save_spec(cwd, task_key, spec)
                    if clear_stale_heavy_workflow(spec, grade):
                        save_spec(cwd, task_key, spec)
                    spec, val_msgs = auto_validate_spec(
                        spec,
                        cwd,
                        time_budget=STOP_JUDGE_BUDGET,
                        transcript_path=input_data.get("transcript_path"),
                    )
                    save_spec(cwd, task_key, spec)
                    # Gap 2 + 7: enrich the Stop digest with HEAVY phase/primary
                    # transitions and sub-agent-credited citations. Both ride
                    # val_msgs, which only surfaces when the completion breaker
                    # blocks -- never on an allow-stop (AGENTS.md additionalContext rule).
                    extra_msgs = _stop_workflow_notes(
                        spec, heavy_before, stop_added,
                        ledger_activity, transcript_activity, cwd,
                    )
                    if extra_msgs:
                        val_msgs = list(val_msgs) + extra_msgs
                    validate_ctx, validate_ctx_truncated = _build_stop_validate_context(
                        spec, val_msgs,
                    )
                    if validate_ctx:
                        full_ctx, _ = _build_stop_validate_context(
                            spec, val_msgs, max_len=1_000_000,
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
                        spec, ok_tasks, incomplete, val_msgs, validate_ctx, early = (
                            _handle_completion_loop_release(
                                input_data, cwd, task_key, spec, _led, incomplete,
                                val_msgs, validate_ctx,
                            )
                        )
                        if early is not None:
                            emit_json(early)
                            return 0
                        validate_ctx, validate_ctx_truncated = _build_stop_validate_context(
                            spec, val_msgs,
                        )
                        if validate_ctx:
                            full_ctx, _ = _build_stop_validate_context(
                                spec, val_msgs, max_len=1_000_000,
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
                            f"breaker CLOSED: {len(incomplete)} task(s) not validated ({', '.join(incomplete)})."
                        )
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
                        if loop_lift_ctx:
                            ev_reason += "\n\n" + loop_lift_ctx
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
                        from spec import format_spec_validation_block

                        ev_reason = format_spec_validation_block(
                            grade,
                            reasons,
                            resolve_evidence_profile(ledger, spec),
                            spec,
                        )
                    else:
                        # Citation truth-check: code-profile tasks only.
                        try:
                            from citations import (activity_from_ledger, enabled,
                                                   filter_gate_defect_citation_reasons,
                                                   format_citation_verify_message,
                                                   merge_activity, scan_transcript,
                                                   verify_citations)
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

    # 2. Goal-mode prompt Stop hook. mirrors Cursor's prompt hook shape, but uses
    #    gpt-realtime-2 through codex_judge instead of Haiku.
    try:
        goal_payload = goal_stop_decision(input_data, cwd)
        if goal_payload:
            _emit_stop_payload(
                goal_payload,
                input_data,
                validate_ctx=validate_ctx,
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

    # 4. Promise-no-act guard: if the agent's final text promises future action
    #    without a tool call or user handoff question, force one continuation.
    reason = promise_no_act_reason(input_data)
    if reason:
        emit_json({"decision": "block", "reason": reason})
        return 0

    # 5. Findings cross-link (opt-in: empty unless .unifable/findings.json exists):
    #    open high/critical findings block completion. Fails open.
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
                "reason": f"{len(blockers)} open high/critical finding(s) to resolve or reject "
                f"before completing: {ids}.",
            }
        )
        return 0

    # 6. Observation gate — should_block_stop (deep changed-but-unverified). This
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
            emit_json(
                {"decision": "block", "reason": obs_reason}
            )
            return 0

    warning = warning_after_max_blocks(ledger)
    if warning:
        emit_json({"systemMessage": warning})
    else:
        emit_json({})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        emit_json({"systemMessage": f"unifable gate stop hook failed open: {exc}"})
        raise SystemExit(0)
