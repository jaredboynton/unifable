#!/usr/bin/env python3
"""Single-purpose judge-backed grade classifier.

gpt-realtime-2 classifies the operative user prompt into a task mode
(quick / normal / deep) that sets the enforcement grade (LIGHT / STANDARD /
HEAVY). Replaces the legacy deterministic word-match classifier, which was too
aggressive ("refactor" -> HEAVY on a 3-line tweak) and too brittle.

Called once per UserPromptSubmit from gate_prompt.py. Fails open to
normal/STANDARD on any judge/transport error.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

try:
    from evidence_policy import DEFAULT_EVIDENCE_PROFILE, EVIDENCE_PROFILES, grade_for_mode, MODES
    from spec import load_spec, resolve_session_id, save_spec
except ImportError:  # pragma: no cover
    from scripts.gate.evidence_policy import (
        DEFAULT_EVIDENCE_PROFILE,
        EVIDENCE_PROFILES,
        MODES,
        grade_for_mode,
    )
    from scripts.gate.spec import load_spec, resolve_session_id, save_spec

_GRADE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "enum": list(MODES),
            "description": (
                "quick: trivial question, explanation, read-only review, one-line answer. "
                "normal: focused fix, bug fix, feature, test addition, routine refactor, "
                "executing an approved plan. deep: genuinely architectural scope -- "
                "production migration, auth/security overhaul, multi-system design, "
                "unknown approach space."
            ),
        },
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Short tags for risks the gates should know about: 'uncertainty' for "
                "hedging language, 'production-deploy', 'auth-touch', etc. Empty if none."
            ),
        },
        "reason": {
            "type": "string",
            "description": "One sentence explaining the classification.",
        },
        "evidence_profile": {
            "type": "string",
            "enum": list(EVIDENCE_PROFILES),
            "description": (
                "code: repo edits, tests, harness work, implementation. operational: "
                "research/synthesis/drafting with internal tools -- account lookup, "
                "transcript review, Slack/email reply drafting, CRM summaries."
            ),
        },
    },
    "required": ["mode", "risk_flags", "reason", "evidence_profile"],
    "additionalProperties": False,
}

_GRADE_SYSTEM = (
    "You classify an autonomous coding agent's prompt into a task mode that sets the "
    "enforcement grade. Classify based on the OPERATIVE USER MESSAGE primarily. The "
    "restated goal and task board are CONTEXT ONLY (to understand what work is in "
    "progress) -- task titles or descriptions in the board must NEVER drive the mode "
    "classification.\n"
    "Return ONE mode:\n"
    "- quick (LIGHT): trivial question, explanation, read-only review, 'just explain', "
    "one-line answer, bare yes/no. Waives the evidence spec entirely.\n"
    "- normal (STANDARD): focused fix, bug fix, feature implementation, test addition, "
    "routine refactor, editing an already-approved plan into code. Needs the evidence "
    "spec but no architectural exploration.\n"
    "- deep (HEAVY): genuinely architectural scope -- production migration, "
    "auth/security overhaul, multi-system design where exploring rejected alternatives "
    "first adds real value, unknown approach space. Adds frontier-first workflow.\n"
    "DECISION RULES:\n"
    "- Editing code on an approved/bounded plan is NORMAL, not DEEP, even if it touches "
    "'auth', 'security', 'production', or 'refactor' code paths. Those words describe "
    "what code is being touched, not the task's architectural scope.\n"
    "- The 'uncertainty' risk flag ONLY prevents 'quick' (hedging needs research, so "
    "do not waive the spec). It MUST NEVER push toward 'deep'. Hedging means 'this "
    "might need some research,' not 'this is architecturally complex.'\n"
    "- Bare continuation words ('proceed', 'continue', 'go ahead', 'yes', 'ok', 'keep "
    "going', 'next') are NEVER deep. They inherit the ongoing work. If the operative "
    "message is a short continuation or acknowledgment, classify as 'normal'.\n"
    "- Explicit operator language ('this is a normal task', 'quick question', 'waive "
    "HEAVY', 'manual override to normal') is a strong signal -- obey it.\n"
    "- When ambiguous, prefer normal over deep. HEAVY is for genuine architectural "
    "exploration only, not for any task that happens to touch sensitive code.\n"
    "EXAMPLES:\n"
    "- 'What does this config key do?' -> quick (read-only explanation, no edits).\n"
    "- 'Fix the failing auth test' -> normal (bounded fix; 'auth' does not make it deep).\n"
    "Also return evidence_profile:\n"
    "- operational: research/synthesis/drafting where the deliverable is prose, a "
    "reply, or a summary -- account lookup, CRM/Salesforce research, Gong/Slack "
    "transcript review, draft email/Slack response, propose a customer reply. "
    "Classify as normal (not deep). Operational work is NEVER deep/HEAVY.\n"
    "- code: file edits in the repo, bug fixes, features, refactors, tests, harness "
    "self-work. Default when ambiguous between code and operational.\n"
    "- Repo-internal maintenance (version bump via just version, plugin.json/marketplace "
    "manifest sync, setup.sh release tail) stays code profile but needs only in-repo "
    "repo_context (AGENTS.md, justfile, scripts/bump_version.py) -- never require "
    "SemVer.org or other external prior_art for those bounded tasks.\n"
    "risk_flags: free-form short tags for risks the gates should know about. Empty "
    "array if none. reason: one sentence."
)

_JUDGE_TIMEOUT = float(os.environ.get("UNIFABLE_GRADE_JUDGE_TIMEOUT", "90"))


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
    restated_goal: str,
    task_summary: list[dict[str, str]] | None,
) -> str:
    return json.dumps(
        {
            "operative_user_message": operative,
            "restated_goal": (restated_goal or "")[:500],
            "tasks": task_summary or [],
        },
        ensure_ascii=False,
    )


JudgeFn = Callable[..., dict[str, Any] | None]


def judge_grade_classify(
    operative: str,
    *,
    restated_goal: str = "",
    task_summary: list[dict[str, str]] | None = None,
    judge_fn: JudgeFn | None = None,
) -> dict[str, Any] | None:
    """Classify the prompt into {mode, risk_flags, reason}. Returns None on failure."""
    if not operative.strip():
        return None
    if judge_fn is not None:
        try:
            return judge_fn(operative, restated_goal=restated_goal, task_summary=task_summary)
        except Exception:
            return None
    try:
        from codex_judge import JudgeError, ask_structured
    except ImportError:  # pragma: no cover
        from scripts.gate.codex_judge import JudgeError, ask_structured

    try:
        return ask_structured(
            _GRADE_SYSTEM,
            _judge_user(operative, restated_goal=restated_goal, task_summary=task_summary),
            _GRADE_SCHEMA,
            schema_name="grade_classify",
            timeout=_JUDGE_TIMEOUT,
        )
    except (JudgeError, Exception):
        return None


def parse_grade_verdict(
    verdict: dict[str, Any] | None,
) -> tuple[str, list[str], str, str]:
    """Coerce a raw judge verdict into (mode, risk_flags, reason, evidence_profile).

    Returns ('normal', [], '', 'code') on any parse failure -- the fail-open default."""
    if not isinstance(verdict, dict):
        return "normal", [], "", DEFAULT_EVIDENCE_PROFILE
    mode = str(verdict.get("mode") or "").lower().strip()
    if mode not in MODES:
        mode = "normal"
    raw_flags = verdict.get("risk_flags")
    flags = (
        [str(f).strip() for f in raw_flags if str(f).strip()]
        if isinstance(raw_flags, list)
        else []
    )
    reason = str(verdict.get("reason") or "").strip()
    profile = str(verdict.get("evidence_profile") or "").lower().strip()
    if profile not in EVIDENCE_PROFILES:
        profile = DEFAULT_EVIDENCE_PROFILE
    if profile == "operational" and mode == "deep":
        mode = "normal"
        if reason:
            reason = f"{reason} (operational coerced from deep to normal)"
        else:
            reason = "operational work coerced from deep to normal"
    return mode, flags, reason, profile


# ---------------------------------------------------------------------------
# Ledger / spec application (used by gate_prompt.py and operator overrides)
# ---------------------------------------------------------------------------

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


def apply_classified_grade_ledger(
    ledger: dict[str, Any],
    mode: str,
    reason: str,
    *,
    by: str = "judge",
    evidence_profile: str | None = None,
) -> None:
    """Set the classified mode/grade on the ledger. by='judge' for the classifier,
    'operator' for a manual override."""
    m = (mode or "normal").lower().strip()
    if m not in MODES:
        m = "normal"
    profile = str(evidence_profile or DEFAULT_EVIDENCE_PROFILE).lower().strip()
    if profile not in EVIDENCE_PROFILES:
        profile = DEFAULT_EVIDENCE_PROFILE
    ledger["task_mode"] = m
    ledger["grade"] = grade_for_mode(m)
    ledger["evidence_profile"] = profile
    ledger["grade_override_applied"] = True
    ledger["grade_override_target"] = grade_for_mode(m)
    ledger["grade_override_by"] = (by or "judge").strip()[:32]
    ledger["grade_override_reason"] = (reason or "").strip()[:500]
    ledger["inject_heavy_brief"] = False


# Back-compat alias for tests and any external callers.
def apply_grade_override_ledger(
    ledger: dict[str, Any],
    target_mode: str,
    reason: str,
    *,
    by: str = "judge",
) -> None:
    apply_classified_grade_ledger(ledger, target_mode, reason, by=by)


def format_override_context(mode: str, reason: str, *, by: str = "judge") -> str:
    grade = grade_for_mode(mode)
    detail = reason.strip() or "grade classified"
    source = "judge grade classification" if by == "judge" else "operator override"
    return f"unifable: task mode {mode} ({grade}) by {source}. {detail}"
