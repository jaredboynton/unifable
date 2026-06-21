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

from ledger import emit_json, load_ledger, read_stdin_json, save_ledger
from verify_state import MAX_STOP_BLOCKS, should_block_stop, warning_after_max_blocks


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
    """The spec-gate grade recorded for this session by gate_prompt.py."""
    try:
        return load_ledger(input_data).get("grade") or "STANDARD"
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
    cwd = input_data.get("cwd") or os.getcwd()
    grade = (os.environ.get("UNIFABLE_GRADE") or ledger_grade(input_data) or "STANDARD").upper().strip()

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

            # Active spec key: the prompt-hash task gate_prompt.py pinned in the
            # ledger (locked until complete), else session_id / host env. None ->
            # nothing resolvable -> fail open (skip the gate).
            try:
                task_key = load_ledger(input_data).get("active_task")
            except Exception:
                task_key = None
            if not task_key:
                task_key = resolve_session_id(input_data, default=None)
            spec = load_spec(cwd, task_key) if task_key else None
            ev_reason = ""
            if task_key and spec is None:
                ev_reason = (
                    "no evidence spec for this task: create one with `python3 scripts/gate/spec.py "
                    f"create --task-id {task_key} --goal '<goal>' --task 'title::<check>' "
                    "--repo-context 'path:line::why' --prior-art '<url>::why'` before finishing."
                )
            elif spec is not None:
                # Breaker: a task-spec must have EVERY task validated (its check ran
                # AND the judge confirmed) before the breaker opens. Blocks every
                # stop until then.
                ok_tasks, incomplete = all_tasks_validated(spec)
                if not ok_tasks:
                    ev_reason = (
                        f"breaker CLOSED: {len(incomplete)} task(s) not validated ({', '.join(incomplete)}). "
                        f"Run `python3 scripts/gate/spec.py validate-task --task-id {task_key} --task <id>` "
                        "for each until the judge passes it."
                    )
                else:
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

    # 2. Loop guard for the softer gates below (findings + observation): never
    #    block twice in a row on the same stop.
    if input_data.get("stop_hook_active") is True:
        emit_json({})
        return 0

    # 3. Promise-no-act guard: if the agent's final text promises future action
    #    without a tool call or user handoff question, force one continuation.
    reason = promise_no_act_reason(input_data)
    if reason:
        emit_json({"decision": "block", "reason": reason})
        return 0

    # 4. Findings cross-link (opt-in: empty unless .unifable/findings.json exists):
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

    # 5. Observation gate — should_block_stop (deep changed-but-unverified). This
    #    softer nudge keeps the MAX_STOP_BLOCKS cap + holdout, so it never traps.
    ledger = load_ledger(input_data)
    block, obs_reason = should_block_stop(ledger)
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
