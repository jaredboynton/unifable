#!/usr/bin/env python3
"""unifable completion gate — Stop.

Blocks completion in priority order, capped at MAX_STOP_BLOCKS reminders then
allows with a warning (never traps); fails open:

  1. Evidence gate (unconditional, no env disable): on a non-LIGHT task the
     evidence spec must EXIST and validate — the agent is required to write its
     evidence back (restated_goal, acceptance_criteria with live output,
     must_read {cite,why}, prior_art URL) before finishing. No spec, or a
     placeholder/invalid one, blocks. A stop with no session_id fails open.
  2. Observation gate: a non-quick, non-docs task that changed files but has no
     observed successful verification.

Runs alongside finish-the-work.sh (promise-no-act guard): the two cover
different failure modes — claim-without-observation vs intent-without-action.
"""

from __future__ import annotations

import os
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


def main() -> int:
    input_data = read_stdin_json()
    # Respect the loop guard so we never block twice in a row on the same stop.
    if input_data.get("stop_hook_active") is True:
        emit_json({})
        return 0

    cwd = input_data.get("cwd") or os.getcwd()

    # Findings cross-link (opt-in: empty unless .unifable/findings.json exists):
    # open high/critical findings block completion. Fails open.
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
                f"before completing: {ids}. See packs/memory-closure.md.",
            }
        )
        return 0

    ledger = load_ledger(input_data)

    # The strongest blocking reason for this stop, in priority order:
    #   1. evidence gate (unconditional, no env disable) — on a non-LIGHT task the
    #      evidence spec must EXIST and validate (must_read {cite,why},
    #      acceptance_criteria with live output, prior_art URL). No spec, or an
    #      invalid/placeholder spec, blocks completion: the agent is required to
    #      write its evidence back before finishing.
    #   2. observation gate — should_block_stop (deep changed-but-unverified).
    # Both honour the stop-block cap + holdout below, so the agent is nudged at
    # most MAX_STOP_BLOCKS times and is never trapped. A stop event with no
    # session_id (malformed/empty input) skips the evidence gate and fails open.
    session_id = input_data.get("session_id")
    grade = (os.environ.get("UNIFABLE_GRADE") or ledger_grade(input_data) or "STANDARD").upper().strip()

    reason = ""
    if session_id and grade != "LIGHT":
        try:
            from spec import load_spec, validate_spec

            spec = load_spec(cwd, session_id)
            if spec is None:
                reason = (
                    "no evidence spec for this task: write .unifable/spec/<session>.json documenting "
                    "restated_goal, acceptance_criteria (with live command output), must_read "
                    "{cite,why}, and a prior_art URL before finishing."
                )
            else:
                ok, reasons = validate_spec(spec, grade, require_evidence=True)
                if not ok:
                    reason = "evidence spec invalid at completion (placeholder/missing evidence): " + "; ".join(reasons)
        except Exception:
            reason = ""

    if not reason:
        block, obs_reason = should_block_stop(ledger)
        if block:
            reason = obs_reason

    if reason:
        # M3 holdout (env-gated, default off): 'off' arm sessions skip the gate
        # so we can measure the gate's effect against a pure baseline.
        if _holdout_suppresses(input_data):
            _log_holdout(input_data, reason)
            emit_json({})
            return 0
        # Cap: nudge at most MAX_STOP_BLOCKS times, then fall through to the
        # advisory warning so the agent is never trapped.
        if int(ledger.get("stop_blocks") or 0) < MAX_STOP_BLOCKS:
            ledger["stop_blocks"] = int(ledger.get("stop_blocks") or 0) + 1
            save_ledger(input_data, ledger)
            emit_json(
                {"decision": "block", "reason": reason + " See packs/memory-closure.md for the pre-completion checklist."}
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
