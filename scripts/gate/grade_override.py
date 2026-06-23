#!/usr/bin/env python3
"""Judge-backed manual HEAVY downgrade for operational/prose tasks.

When the operator sends an explicit override message, gpt-realtime-2 decides
whether to lift HEAVY to quick/normal. Fail open on any judge/transport error.
"""

from __future__ import annotations

from typing import Any

try:
    from classify_task import operative_prompt
    from evidence_policy import MODES, grade_for_mode
    from spec import load_spec, resolve_session_id, save_spec
except ImportError:  # pragma: no cover
    from scripts.gate.classify_task import operative_prompt
    from scripts.gate.evidence_policy import MODES, grade_for_mode
    from scripts.gate.spec import load_spec, resolve_session_id, save_spec

_OVERRIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "apply_override": {"type": "boolean"},
        "target_mode": {"type": "string", "enum": list(MODES)},
        "reason": {"type": "string"},
    },
    "required": ["apply_override", "target_mode", "reason"],
    "additionalProperties": False,
}

_OVERRIDE_SYSTEM = (
    "You are a gate for the unifable harness. The operator may explicitly request "
    "to lift HEAVY enforcement because the task is operational prose (dispatch, "
    "report, Confluence edit, voice pass) rather than architectural code work. "
    "Return apply_override=true only when the operative user text clearly requests "
    "downgrading HEAVY or states the task is NORMAL/quick prose work. When "
    "ambiguous, return apply_override=false. target_mode should be 'normal' for "
    "standard spec-gated prose work, 'quick' only when they explicitly want a "
    "trivial/light response."
)


def _judge_user(
    operative: str,
    *,
    current_mode: str,
    current_grade: str,
    restated_goal: str,
) -> str:
    return (
        f"Operative user message:\n{operative}\n\n"
        f"Current task_mode: {current_mode or 'unknown'}\n"
        f"Current grade: {current_grade or 'unknown'}\n"
        f"Restated goal (snippet): {restated_goal[:500]}\n"
    )


def judge_grade_override(
    operative: str,
    *,
    current_mode: str = "",
    current_grade: str = "",
    restated_goal: str = "",
) -> dict[str, Any] | None:
    """Call gpt-realtime-2; return parsed verdict or None on failure."""
    try:
        from codex_judge import JudgeError, ask_structured
    except ImportError:  # pragma: no cover
        from scripts.gate.codex_judge import JudgeError, ask_structured

    try:
        return ask_structured(
            _OVERRIDE_SYSTEM,
            _judge_user(
                operative,
                current_mode=current_mode,
                current_grade=current_grade,
                restated_goal=restated_goal,
            ),
            _OVERRIDE_SCHEMA,
            schema_name="grade_override",
        )
    except JudgeError:
        return None
    except Exception:
        return None


def clear_heavy_spec_fields(spec: dict[str, Any]) -> None:
    spec["heavy_workflow"] = False
    spec.pop("heavy_phase", None)


def apply_grade_override_to_spec(cwd: str, session_key: str) -> bool:
    try:
        spec = load_spec(cwd, session_key)
        if not isinstance(spec, dict):
            return False
        if not spec.get("heavy_workflow") and "heavy_phase" not in spec:
            return False
        clear_heavy_spec_fields(spec)
        save_spec(cwd, session_key, spec)
        return True
    except Exception:
        return False


def apply_grade_override_ledger(ledger: dict[str, Any], target_mode: str, reason: str) -> None:
    mode = (target_mode or "normal").lower().strip()
    if mode not in MODES:
        mode = "normal"
    ledger["task_mode"] = mode
    ledger["grade"] = grade_for_mode(mode)
    ledger["grade_override_applied"] = True
    ledger["grade_override_reason"] = (reason or "").strip()[:500]
    ledger["inject_heavy_brief"] = False


def format_override_context(target_mode: str, reason: str) -> str:
    grade = grade_for_mode(target_mode)
    detail = reason.strip() or "operator override accepted"
    return (
        f"unifable: HEAVY lifted to {grade} ({target_mode} task mode) by operator override. "
        f"{detail}"
    )


def try_apply_grade_override(
    input_data: dict,
    prompt: str,
    *,
    judge_fn=judge_grade_override,
) -> str:
    """Judge + apply downgrade. Returns additionalContext text or ""."""
    operative = operative_prompt(prompt)
    if not operative.strip():
        return ""

    try:
        from ledger import load_ledger

        ledger = load_ledger(input_data)
    except Exception:
        return ""

    try:
        from spec import canonical_project_root

        cwd = str(canonical_project_root(input_data.get("cwd") or ""))
    except Exception:
        cwd = str(input_data.get("cwd") or "")
    session_key = resolve_session_id(input_data, default="default") or "default"
    spec = load_spec(cwd, session_key) if cwd else None
    restated = ""
    if isinstance(spec, dict):
        restated = str(spec.get("restated_goal") or "")

    current_mode = str(ledger.get("task_mode") or "")
    current_grade = grade_for_mode(current_mode) if current_mode else str(ledger.get("grade") or "")

    verdict = judge_fn(
        operative,
        current_mode=current_mode,
        current_grade=current_grade,
        restated_goal=restated,
    )
    if not isinstance(verdict, dict) or not verdict.get("apply_override"):
        return ""

    target_mode = str(verdict.get("target_mode") or "normal").lower().strip()
    if grade_for_mode(target_mode) == "HEAVY":
        return ""

    reason = str(verdict.get("reason") or "").strip()

    def apply(ledger_mut: dict) -> None:
        apply_grade_override_ledger(ledger_mut, target_mode, reason)

    try:
        from ledger import update_ledger

        update_ledger(input_data, apply)
    except Exception:
        return ""

    if cwd:
        apply_grade_override_to_spec(cwd, session_key)

    return format_override_context(target_mode, reason)
