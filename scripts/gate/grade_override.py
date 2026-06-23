#!/usr/bin/env python3
"""Judge-backed HEAVY grade adjudication and downgrade.

When effective grade is HEAVY, gpt-realtime-2 decides whether frontier-first
enforcement is warranted or the task should run at STANDARD/LIGHT. Fail open on
any judge/transport error.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

try:
    from classify_task import DEEP_RE, operative_prompt
    from evidence_policy import MODES, grade_for_mode, resolve_grade
    from spec import load_spec, resolve_session_id, save_spec
except ImportError:  # pragma: no cover
    from scripts.gate.classify_task import DEEP_RE, operative_prompt
    from scripts.gate.evidence_policy import MODES, grade_for_mode, resolve_grade
    from scripts.gate.spec import load_spec, resolve_session_id, save_spec

_WARRANT_SCHEMA = {
    "type": "object",
    "properties": {
        "warrant_heavy": {"type": "boolean"},
        "target_mode": {"type": "string", "enum": list(MODES)},
        "reason": {"type": "string"},
    },
    "required": ["warrant_heavy", "target_mode", "reason"],
    "additionalProperties": False,
}

_WARRANT_SYSTEM = (
    "You are a gate for the unifable harness. Decide whether frontier-first HEAVY "
    "enforcement is warranted for this task. Keep HEAVY only for genuinely "
    "architectural scope: production migrations, auth/security overhauls, multi-system "
    "design where exploring rejected alternatives first adds real value. Downgrade "
    "(warrant_heavy=false) for focused fixes, harness/plugin self-work, test additions, "
    "incremental refactors, operational prose (dispatch, briefing, Confluence), and "
    "routine implementation without architectural exploration. Explicit operator "
    "language requesting NORMAL/quick or waiving HEAVY is a strong downgrade signal. "
    "When ambiguous, prefer downgrade to normal (STANDARD spec) over HEAVY ceremony. "
    "target_mode should be 'normal' for standard spec-gated work, 'quick' only when "
    "they explicitly want a trivial/light response."
)

_RE_ESCALATE_RE = re.compile(
    r"(?i)\b(escalate to deep|escalate to heavy|warrant heavy|frontier-first|"
    r"resume heavy|restore heavy)\b"
)

_RE_WARRANT_SYSTEM = (
    "The operator previously downgraded this session from HEAVY. They now signal "
    "genuine architectural scope again. Return warrant_heavy=true only when the "
    "operative text clearly requests re-escalation to deep/frontier work or carries "
    "hard production/database/auth migration risks. Otherwise keep warrant_heavy=false."
)


def _task_summary(spec: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(spec, dict):
        return []
    out: list[dict[str, str]] = []
    for task in spec.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        out.append({
            "id": str(task.get("id") or ""),
            "title": str(task.get("title") or "")[:120],
            "kind": str(task.get("approach_kind") or "requirement"),
            "status": str(task.get("status") or ""),
        })
    return out[:20]


def _judge_user(
    operative: str,
    *,
    current_mode: str,
    current_grade: str,
    restated_goal: str,
    risk_flags: list[str] | None = None,
    read_paths: list[str] | None = None,
    task_summary: list[dict[str, str]] | None = None,
    re_warrant: bool = False,
) -> str:
    payload: dict[str, Any] = {
        "operative_user_message": operative,
        "current_task_mode": current_mode or "unknown",
        "current_grade": current_grade or "unknown",
        "restated_goal": (restated_goal or "")[:500],
        "risk_flags": risk_flags or [],
        "recent_read_paths": (read_paths or [])[-20:],
        "tasks": task_summary or [],
        "re_warrant_request": re_warrant,
    }
    return json.dumps(payload, ensure_ascii=False)


def judge_grade_warrant(
    operative: str,
    *,
    current_mode: str = "",
    current_grade: str = "",
    restated_goal: str = "",
    risk_flags: list[str] | None = None,
    read_paths: list[str] | None = None,
    task_summary: list[dict[str, str]] | None = None,
    re_warrant: bool = False,
) -> dict[str, Any] | None:
    """Call gpt-realtime-2; return parsed verdict or None on failure."""
    try:
        from codex_judge import JudgeError, ask_structured
    except ImportError:  # pragma: no cover
        from scripts.gate.codex_judge import JudgeError, ask_structured

    system = _RE_WARRANT_SYSTEM if re_warrant else _WARRANT_SYSTEM
    try:
        return ask_structured(
            system,
            _judge_user(
                operative,
                current_mode=current_mode,
                current_grade=current_grade,
                restated_goal=restated_goal,
                risk_flags=risk_flags,
                read_paths=read_paths,
                task_summary=task_summary,
                re_warrant=re_warrant,
            ),
            _WARRANT_SCHEMA,
            schema_name="grade_warrant",
        )
    except JudgeError:
        return None
    except Exception:
        return None


def judge_grade_override(
    operative: str,
    *,
    current_mode: str = "",
    current_grade: str = "",
    restated_goal: str = "",
) -> dict[str, Any] | None:
    """Legacy alias: operator-biased warrant call."""
    return judge_grade_warrant(
        operative,
        current_mode=current_mode,
        current_grade=current_grade,
        restated_goal=restated_goal,
    )


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


def clear_grade_override_pin(ledger: dict[str, Any]) -> None:
    for key in (
        "grade_override_applied",
        "grade_override_target",
        "grade_override_by",
        "grade_override_reason",
    ):
        ledger.pop(key, None)


def apply_grade_override_ledger(
    ledger: dict[str, Any],
    target_mode: str,
    reason: str,
    *,
    by: str = "judge",
) -> None:
    mode = (target_mode or "normal").lower().strip()
    if mode not in MODES:
        mode = "normal"
    ledger["task_mode"] = mode
    ledger["grade"] = grade_for_mode(mode)
    ledger["grade_override_applied"] = True
    ledger["grade_override_target"] = grade_for_mode(mode)
    ledger["grade_override_by"] = (by or "judge").strip()[:32]
    ledger["grade_override_reason"] = (reason or "").strip()[:500]
    ledger["inject_heavy_brief"] = False


def apply_re_warrant_ledger(ledger: dict[str, Any], reason: str) -> None:
    clear_grade_override_pin(ledger)
    ledger["task_mode"] = "deep"
    ledger["grade"] = "HEAVY"
    ledger["inject_heavy_brief"] = False
    if reason.strip():
        ledger["grade_re_warrant_reason"] = reason.strip()[:500]


def format_override_context(target_mode: str, reason: str, *, by: str = "judge") -> str:
    grade = grade_for_mode(target_mode)
    detail = reason.strip() or "grade adjudication accepted"
    source = "judge grade adjudication" if by == "judge" else "operator override"
    return (
        f"unifable: HEAVY lifted to {grade} ({target_mode} task mode) by {source}. "
        f"{detail}"
    )


def format_re_warrant_context(reason: str) -> str:
    detail = reason.strip() or "architectural scope confirmed"
    return f"unifable: grade pin cleared; HEAVY re-warranted by judge. {detail}"


def _wants_re_escalation(operative: str, risk_flags: list[str] | None) -> bool:
    flags = risk_flags or []
    hard = [r for r in flags if r != "uncertainty"]
    if any(r in hard for r in ("production", "database", "remote-write")):
        return True
    text = operative or ""
    return bool(DEEP_RE.search(text)) or bool(_RE_ESCALATE_RE.search(text))


def _collect_context(
    input_data: dict,
    prompt: str,
) -> tuple[dict, str, str, str, dict[str, Any] | None, str]:
    try:
        from ledger import load_ledger

        ledger = load_ledger(input_data)
    except Exception:
        return {}, "", "", "default", None, ""

    try:
        from spec import canonical_project_root

        cwd = str(canonical_project_root(input_data.get("cwd") or ""))
    except Exception:
        cwd = str(input_data.get("cwd") or "")
    session_key = resolve_session_id(input_data, default="default") or "default"
    spec = load_spec(cwd, session_key) if cwd else None
    operative = operative_prompt(prompt)
    return ledger, cwd, session_key, operative, spec, cwd


def _apply_downgrade(
    input_data: dict,
    cwd: str,
    session_key: str,
    target_mode: str,
    reason: str,
    *,
    by: str = "judge",
) -> str:
    def apply(ledger_mut: dict) -> None:
        apply_grade_override_ledger(ledger_mut, target_mode, reason, by=by)

    try:
        from ledger import update_ledger

        update_ledger(input_data, apply)
    except Exception:
        return ""

    if cwd:
        apply_grade_override_to_spec(cwd, session_key)

    return format_override_context(target_mode, reason, by=by)


def try_adjudicate_grade(
    input_data: dict,
    prompt: str,
    *,
    judge_fn=judge_grade_warrant,
) -> str:
    """Judge whether HEAVY is warranted; apply downgrade when not. Returns context or ""."""
    ledger, cwd, session_key, operative, spec, _ = _collect_context(input_data, prompt)
    if not ledger:
        return ""

    env_grade = os.environ.get("UNIFABLE_GRADE")
    restated = str(spec.get("restated_goal") or "") if isinstance(spec, dict) else ""
    risk_flags = list(ledger.get("risk_flags") or [])
    read_paths = list(ledger.get("read_paths") or [])
    task_summary = _task_summary(spec)
    current_mode = str(ledger.get("task_mode") or "")
    current_grade = resolve_grade(ledger, env_grade)

    pinned = bool(
        ledger.get("grade_override_applied") and ledger.get("grade_override_target")
    )

    if pinned and _wants_re_escalation(operative, risk_flags):
        verdict = judge_fn(
            operative,
            current_mode=current_mode,
            current_grade=current_grade,
            restated_goal=restated,
            risk_flags=risk_flags,
            read_paths=read_paths,
            task_summary=task_summary,
            re_warrant=True,
        )
        if isinstance(verdict, dict) and verdict.get("warrant_heavy"):
            reason = str(verdict.get("reason") or "").strip()

            def re_apply(ledger_mut: dict) -> None:
                apply_re_warrant_ledger(ledger_mut, reason)

            try:
                from ledger import update_ledger

                update_ledger(input_data, re_apply)
            except Exception:
                return ""
            return format_re_warrant_context(reason)

    if current_grade != "HEAVY":
        return ""

    verdict = judge_fn(
        operative,
        current_mode=current_mode,
        current_grade=current_grade,
        restated_goal=restated,
        risk_flags=risk_flags,
        read_paths=read_paths,
        task_summary=task_summary,
    )
    if not isinstance(verdict, dict) or verdict.get("warrant_heavy"):
        return ""

    target_mode = str(verdict.get("target_mode") or "normal").lower().strip()
    if grade_for_mode(target_mode) == "HEAVY":
        return ""

    reason = str(verdict.get("reason") or "").strip()
    return _apply_downgrade(input_data, cwd, session_key, target_mode, reason, by="judge")


def try_apply_grade_override(
    input_data: dict,
    prompt: str,
    *,
    judge_fn=judge_grade_warrant,
) -> str:
    """Legacy entry: proactive adjudication (operator phrases are judge signals)."""
    return try_adjudicate_grade(input_data, prompt, judge_fn=judge_fn)
