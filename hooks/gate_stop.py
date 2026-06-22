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
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "scripts" / "gate"))
sys.path.insert(0, str(_HERE.parent / "scripts" / "shadow"))

from atomicio import write_text_atomic
from evidence_policy import resolve_grade
from ledger import emit_json, load_ledger, read_stdin_json, save_ledger
from transcript_tail import TRANSCRIPT_TOKEN_BUDGET, stripped_transcript_tail
from verify_state import MAX_STOP_BLOCKS, should_block_stop, warning_after_max_blocks

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

    Bumps the persistent consecutive-block counter; once it crosses the threshold
    (and every STEP blocks after), spends one judge call for a concrete next step.
    Returns hint text to append to the still-blocking reason, or "" when it is not
    time to nudge or the judge is silent. NEVER lifts the gate; fails open."""
    try:
        ledger = load_ledger(input_data)
        count = int(ledger.get("completion_stop_blocks") or 0) + 1
        ledger["completion_stop_blocks"] = count
        save_ledger(input_data, ledger)
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


def _reset_completion_blocks(input_data: dict) -> None:
    """Clear the consecutive-block counter once the completion breaker opens."""
    try:
        ledger = load_ledger(input_data)
        if ledger.get("completion_stop_blocks"):
            ledger["completion_stop_blocks"] = 0
            save_ledger(input_data, ledger)
    except Exception:
        pass


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
    from spec import canonical_project_root

    cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))
    grade = resolve_grade(load_ledger(input_data), os.environ.get("UNIFABLE_GRADE"))

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

            # Spec key = the session (one spec per directory+session). None ->
            # nothing resolvable -> fail open (skip the gate).
            task_key = resolve_session_id(input_data, default=None)
            spec = load_spec(cwd, task_key) if task_key else None
            ev_reason = ""
            if task_key and spec is None:
                ev_reason = (
                    "no evidence spec for this session (the prompt hook auto-creates one for "
                    "non-trivial work). Add a requirement with `unifable "
                    "add-task --title '<requirement>' --check '<runnable check>'`."
                )
            elif spec is not None:
                try:
                    from citations import (activity_from_ledger, enabled,
                                           merge_activity, scan_transcript,
                                           sync_citations_from_activity, verify_citations)
                    from heavy_workflow import advance_primary_if_ready
                    from spec import auto_validate_spec, save_spec

                    activity = merge_activity(
                        activity_from_ledger(load_ledger(input_data)),
                        scan_transcript(input_data.get("transcript_path")),
                    )
                    if sync_citations_from_activity(spec, activity, cwd):
                        save_spec(cwd, task_key, spec)
                    if advance_primary_if_ready(spec):
                        save_spec(cwd, task_key, spec)
                    spec, _val_msgs = auto_validate_spec(spec, cwd, time_budget=STOP_JUDGE_BUDGET)
                    save_spec(cwd, task_key, spec)
                except Exception:
                    pass  # fail open
                # Breaker: a task-spec must have EVERY task validated (its check ran
                # AND the judge confirmed) before the breaker opens. Blocks every
                # stop until then.
                ok_tasks, incomplete = all_tasks_validated(spec)
                if not ok_tasks:
                    ev_reason = (
                        f"breaker CLOSED: {len(incomplete)} task(s) not validated ({', '.join(incomplete)}). "
                        "Complete the remaining work; the harness re-runs checks on each stop "
                        "until the judge passes every requirement."
                    )
                    # Advisory nudge if the agent has been stuck here repeatedly.
                    # Rides alongside the block; it does NOT lift the breaker.
                    hint = _completion_stop_hint(input_data, spec, incomplete)
                    if hint:
                        ev_reason += (
                            "\n\nHint (advisory, does not lift the gate): " + hint
                        )
                else:
                    _reset_completion_blocks(input_data)
                    ok, reasons = validate_spec(spec, grade, require_evidence=True)
                    if not ok:
                        ev_reason = "evidence spec invalid at completion (placeholder/missing evidence): " + "; ".join(reasons)
                    else:
                        # Citation truth-check: every repo_context / prior_art / acceptance
                        # citation must be backed by real session activity, sourced from
                        # the ledger UNION the transcript (which recurses sub-agent
                        # transcripts, so research delegated to sub-agents counts).
                        # require_commands=True: at completion the checks must have run.
                        try:
                            from citations import (activity_from_ledger, enabled,
                                                   merge_activity, scan_transcript,
                                                   verify_citations)
                            if enabled():
                                activity = merge_activity(
                                    activity_from_ledger(load_ledger(input_data)),
                                    scan_transcript(input_data.get("transcript_path")),
                                )
                                cited = verify_citations(spec, activity, cwd, require_commands=True)
                                if cited:
                                    ev_reason = "spec citations not backed by real activity: " + "; ".join(cited)
                        except Exception:
                            pass  # fail open
            if ev_reason:
                # M3 holdout (env-gated, default off): 'off' arm skips the gate so
                # the gate's effect can be measured against a pure baseline.
                if _holdout_suppresses(input_data):
                    _log_holdout(input_data, ev_reason)
                    emit_json({})
                    return 0
                emit_json(
                    {"decision": "block",
                     "reason": ev_reason + " See packs/completion-checklist.md for the pre-completion checklist."}
                )
                return 0
        except Exception:
            pass  # fail open — a gate bug never interrupts the host

    # 2. Goal-mode prompt Stop hook. mirrors Cursor's prompt hook shape, but uses
    #    gpt-realtime-2 through codex_judge instead of Haiku.
    try:
        goal_payload = goal_stop_decision(input_data, cwd)
        if goal_payload:
            emit_json(goal_payload)
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
                f"before completing: {ids}. See packs/completion-checklist.md.",
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
                {"decision": "block", "reason": obs_reason + " See packs/completion-checklist.md for the pre-completion checklist."}
            )
            return 0

    warning = warning_after_max_blocks(ledger)
    if warning:
        emit_json(
            {
                "systemMessage": warning,
                "hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": warning},
            }
        )
    else:
        emit_json({})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        emit_json({"systemMessage": f"unifable gate stop hook failed open: {exc}"})
        raise SystemExit(0)
