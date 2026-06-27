#!/usr/bin/env python3
"""Judge prompts, schemas, and structured-judge calls for the spec gate (unifable).

All gpt-realtime-2 adjudication of requirement tasks: system prompts, JSON schemas,
verdict normalization, and the judge_* entry points (validate / dispute / discover /
heal / frontier comparison). Judge transport is imported lazily so the module loads
without a live judge. Host-agnostic; re-exported by the spec.py facade.
"""

from __future__ import annotations

import json
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from heavy_workflow import (
        advance_primary_if_ready,
        frontier_tasks,
        primary_task,
        sync_heavy_phase,
    )
    from model_notify import notify_spec_update
    from spec_tasks import (
        RESOLVED_STATUSES,
        _current_requirements_payload,
        append_frontier_task,
    )
except ImportError:  # pragma: no cover
    from scripts.gate.heavy_workflow import (
        advance_primary_if_ready,
        frontier_tasks,
        primary_task,
        sync_heavy_phase,
    )
    from scripts.gate.model_notify import notify_spec_update
    from scripts.gate.spec_tasks import (
        RESOLVED_STATUSES,
        _current_requirements_payload,
        append_frontier_task,
    )


_NEW_REQ_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "check": {"type": "string"},
            "supersedes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "check"],
        "additionalProperties": False,
    },
}


_ADJUST_REQ_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "action": {"type": "string", "enum": ["retract", "revise"]},
            "reason": {"type": "string"},
            "title": {"type": "string"},
            "check": {"type": "string"},
        },
        "required": ["id", "action", "reason"],
        "additionalProperties": False,
    },
}


_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "integer", "enum": [0, 1]},
        "reason": {"type": "string"},
        # The judge may DISCOVER further requirements the goal needs while judging
        # this task. New ones are deduped and the unresolved backlog is capped.
        "new_requirements": _NEW_REQ_SCHEMA,
        # The judge may ADJUST requirements it itself added (retract or revise),
        # listed under existing_judge_requirements in the prompt.
        "adjust_requirements": _ADJUST_REQ_SCHEMA,
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


_DISPUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "integer", "enum": [0, 1]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


_HINT_SCHEMA = {
    "type": "object",
    "properties": {"hint": {"type": "string"}},
    "required": ["hint"],
    "additionalProperties": False,
}


_JUDGE_CORE_GUIDANCE = (
    "Before adding new_requirements, compare PURPOSE against current_requirements "
    "-- what outcome the task enforces when satisfied, not just title wording. "
    "Skip if any existing task (especially validated) already obligates the same "
    "outcome; duplicates trap completion. Include supersedes: [ids] when replacing "
    "broken checks (superseded agent tasks become non-blocking; judge tasks retract). "
    "Prefer adjust_requirements revise over adding a parallel "
    "requirement. Prefer structural manifest/version-field checks over brittle "
    "literal-string or version-pinning requirements; write checks that read version "
    "fields from repo manifests and compare -- a check that fails on every version "
    "bump traps completion. Allow an exact literal or version-pinned check only when "
    "the user task explicitly requires that exact literal. Reject evidence that only "
    "grep-matches a frozen version string when the goal needs a structural manifest "
    "comparison. Judge-added tasks with broken checks must be fixed via adjust_requirements "
    "in THIS response, never by instructing the agent."
)


_JUDGE_FEEDBACK_GUIDANCE = (
    "reason is the only agent-visible feedback. On verdict=0 for agent tasks: "
    "explain why + one concrete next step (read a file, fix code, run a check). "
    "On verdict=1: brief confirmation. Never instruct the agent to fix "
    "judge-owned checks."
)


_JUDGE_HEAL_REASON_BRITTLE = "harness auto-retracted brittle version pin"


_JUDGE_HEAL_SYSTEM = (
    "You self-correct judge-added requirements the coding agent CANNOT fix. "
    "The agent has append-only spec access and cannot edit or retract judge tasks. "
    "Review judge_owned_open and return adjust_requirements ONLY (no "
    "new_requirements): action 'revise' with a runnable shell check when the check "
    "is broken, non-portable, prose, or environment-specific; action 'retract' when "
    "redundant with a validated requirement or unsatisfiable. " + _JUDGE_CORE_GUIDANCE
)


_JUDGE_HEAL_SCHEMA = {
    "type": "object",
    "properties": {
        "adjust_requirements": _ADJUST_REQ_SCHEMA,
        "reason": {"type": "string"},
    },
    "required": ["adjust_requirements"],
    "additionalProperties": False,
}


_HINT_PLACEHOLDERS = ("tbd", "n/a", "none", "no hint", "nothing", "unsure", "unclear")


_HINT_MAX = 280


def _normalize_hint(raw: Any) -> str:
    """Coerce a judge hint into a clean, capped string. Returns '' for anything
    empty or placeholder-like so a non-hint never reaches the agent."""
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    if text.lower() in _HINT_PLACEHOLDERS:
        return ""
    if len(text) > _HINT_MAX:
        text = text[: _HINT_MAX - 3] + "..."
    return text


def _normalize_new_requirements(raw: Any) -> list[dict[str, Any]]:
    """Coerce new_requirements into [{title, check, supersedes?}]."""
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                check = str(item.get("check") or "").strip()
                if not title or not check:
                    continue
                supersedes: list[str] = []
                raw_sup = item.get("supersedes")
                if isinstance(raw_sup, list):
                    supersedes = [str(x).strip() for x in raw_sup if str(x).strip()]
                entry: dict[str, Any] = {"title": title, "check": check}
                if supersedes:
                    entry["supersedes"] = supersedes
                out.append(entry)
    return out


_JUDGE_SYSTEM = (
    "You are a strict, adversarial validator for a software task. "
    "Given the goal, one task with its check, exit code, and output: "
    "verdict 1 only if the output proves genuine completion. "
    "Be skeptical of empty output, errors, skipped tests, and mismatches. "
    "You may ADJUST requirements: 'retract' only for judge-added tasks; "
    "'revise' to fix any broken check; 'supersedes' on new_requirements to replace "
    "agent tasks. Every adjustment is reported to the agent. " + _JUDGE_CORE_GUIDANCE + " " + _JUDGE_FEEDBACK_GUIDANCE
)


_FRONTIER_JUDGE_SYSTEM = (
    "You are a strict frontier-approach adjudicator. A frontier is a realistic "
    "cutting-edge option the agent explores before falling back to the "
    "evidence-backed primary approach. "
    "Given goal, frontier title, check, exit code, and output, decide:\n"
    "- rejected_approach: evidence disqualifies this frontier.\n"
    "- still_viable: more exploration warranted.\n"
    "- accepted_approach: check passed, viable implementation path.\n"
    "Set verdict 1 when the check passed, 0 otherwise. "
    "outcome drives resolution, not verdict.\n" + _JUDGE_CORE_GUIDANCE + " " + _JUDGE_FEEDBACK_GUIDANCE
)


_PRIMARY_JUDGE_SYSTEM = (
    "You are validating delivery of the evidence-backed PRIMARY fallback approach. "
    "This task only runs when all frontier approaches were rejected (none adopted). "
    "Return verdict 1 only if the check output proves the primary approach was "
    "implemented correctly. " + _JUDGE_FEEDBACK_GUIDANCE
)


_DISCOVER_SYSTEM = (
    "You identify realistic cutting-edge frontier approaches worth exploring before "
    "committing to the evidence-backed primary fallback. Given the restated goal, "
    "current_requirements (every prior task with title and check), and recent "
    "research activity (reads, fetches), propose 0-2 frontier approaches. "
    "Each must be plausible, distinct from existing tasks AND from each other by "
    "purpose (not just wording), and testable with a runnable check command. "
    "Do not propose a frontier whose purpose duplicates an existing requirement. "
    "Include scope_paths (repo file paths the frontier would touch) when inferrable. "
    "Return an empty frontiers list if nothing useful to add."
)


_DISCOVER_SCHEMA = {
    "type": "object",
    "properties": {
        "frontiers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "check": {"type": "string"},
                    "scope_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reason": {"type": "string"},
                },
                "required": ["title", "check"],
                "additionalProperties": False,
            },
        },
        "reason": {"type": "string"},
    },
    "required": ["frontiers"],
    "additionalProperties": False,
}


_FRONTIER_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "integer", "enum": [0, 1]},
        "outcome": {"type": "string", "enum": ["rejected_approach", "still_viable", "accepted_approach"]},
        "reason": {"type": "string"},
        "new_requirements": _NEW_REQ_SCHEMA,
        "adjust_requirements": _ADJUST_REQ_SCHEMA,
    },
    "required": ["verdict", "outcome", "reason"],
    "additionalProperties": False,
}


_TASK_VERDICT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "verdict": {"type": "integer", "enum": [0, 1]},
        "reason": {"type": "string"},
        "outcome": {"type": "string"},
        "new_requirements": _NEW_REQ_SCHEMA,
        "adjust_requirements": _ADJUST_REQ_SCHEMA,
    },
    "required": ["id", "verdict", "reason"],
    "additionalProperties": False,
}


_VALIDATE_ALL_SCHEMA = {
    "type": "object",
    "properties": {
        "task_verdicts": {
            "type": "array",
            "items": _TASK_VERDICT_ITEM_SCHEMA,
        },
    },
    "required": ["task_verdicts"],
    "additionalProperties": False,
}


_VALIDATE_ALL_SYSTEM = (
    "You are a strict adversarial validator. Adjudicate ALL open requirements in "
    "ONE pass, plus optional session transcript context. For each entry in "
    "tasks_to_adjudicate:\n"
    "- kind=validate: does check output prove genuine completion? (standard skepticism)\n"
    "- evidence_only=true (a validate entry): the requirement has NO runnable shell "
    "check; its exit_code/output are null BY DESIGN, not a failure. Decide "
    "satisfaction SOLELY from the top-level `evidence` corpus (file reads, URL "
    "fetches, ran commands, captured command outputs, MCP tool results, recorded "
    "verification runs) and the "
    "session transcript. Return "
    "verdict 1 when that captured evidence shows the requirement met; verdict 0 only "
    "when the evidence is absent or contradicts it. Do NOT tell the agent to convert "
    "the check into a shell command or to write a repo file -- a research "
    "requirement is proven by its retrievals, not by a grep.\n"
    "- kind=dispute: accept (verdict 1) only on the dispute-adjudication grounds "
    "below -- proven impossibility OR proven obsolescence (the constrained subject "
    "was removed by a pivot); reject mere difficulty.\n"
    "- approach_kind=frontier: return outcome rejected_approach, still_viable, or "
    "accepted_approach. Verdict 1 when check passed.\n"
    "- approach_kind=primary: validate primary delivery after frontiers ruled out.\n"
    "Return task_verdicts (same fields as single-task validation). " + _JUDGE_CORE_GUIDANCE + " "
    "You may ADJUST requirements via adjust_requirements on any task verdict. " + _JUDGE_FEEDBACK_GUIDANCE
)


_DISPUTE_ADJUDICATION = (
    "For kind=dispute: accept (verdict 1) if impossibility_evidence genuinely proves "
    "impossibility OR proven obsolescence (see OBSOLESCENCE below); reject "
    "(verdict 0) if merely hard or inconvenient. "
    "When session_context.plan_mode_enabled is true, accept disputes where "
    "evidence shows the check requires repo-tracked mutation that host Plan Mode "
    "forbade for this turn."
)


_DISPUTE_OBSOLETE_RULE = (
    "OBSOLESCENCE: also accept (verdict 1) when the requirement constrains a "
    "specific behavior, route, file, or code path that was REMOVED because the "
    "implementation pivoted, AND the impossibility_evidence contains a failable check "
    "(e.g. a repo grep) whose captured output proves that subject is now ABSENT "
    "(zero matches, or the file is gone). A requirement whose subject no longer "
    "exists is obsolete and cannot be satisfied -- a real blocker, not an excuse. "
    "Still reject (verdict 0) when the subject still exists in the repo, when the "
    "agent merely preferred a different approach without removing the old one, or "
    "when no captured absence proof is provided."
)


_PLAN_MODE_JUDGE_RULES = (
    "When plan_mode_enabled is true: mutating repo-tracked files is forbidden. "
    "Expected deliverables: Codex <proposed_plan>; Claude ~/.claude/plans via "
    "ExitPlanMode; Cursor ~/.cursor/plans or CreatePlan output. "
    "For kind=dispute accept when evidence shows repo-file checks are impossible "
    "this turn. For kind=validate do not fail solely on missing repo files when "
    "the check targets repo output Plan Mode prevented; prefer adjust_requirements "
    "revise to a plan-based check or accept a valid dispute. "
    "Do not add new_requirements requiring repo edits while plan mode is active."
)


def _plan_mode_judge_section(plan_mode: dict[str, Any] | None) -> str:
    if not isinstance(plan_mode, dict) or not plan_mode.get("enabled"):
        return ""
    host = str(plan_mode.get("host") or "host")
    marker = str(plan_mode.get("marker") or "")
    return f"\n\n--- PLAN MODE ({host}) ---\nplan_mode_enabled: true\nmarker: {marker}\n" + _PLAN_MODE_JUDGE_RULES + "\n"


def _session_context_payload(plan_mode: dict[str, Any] | None) -> dict[str, Any]:
    pm = plan_mode if isinstance(plan_mode, dict) else {}
    return {
        "plan_mode_enabled": bool(pm.get("enabled")),
        "plan_mode_host": str(pm.get("host") or ""),
    }


_JUDGE_TRANSCRIPT_SECTION = (
    "\n\n--- SESSION TRANSCRIPT (context only; not proof) ---\n"
    "Stripped tail of the agent session: tool results, hook outputs, and "
    "conversation. Use it to understand what the model did and to avoid "
    "re-deriving requirements already satisfied in current_requirements or "
    "evidenced here. Return verdict 1 only when the check output proves the "
    "task; transcript context alone is never sufficient proof -- EXCEPT for "
    "evidence_only requirements, which have no runnable check and ARE adjudicated "
    "from the top-level evidence corpus plus this transcript.\n\n"
)


def _render_judge_transcript(transcript_path: str | None) -> str:
    """Render stripped transcript tail for requirement-validation judges."""
    tail, _pm = _judge_context(transcript_path)
    return tail


def _judge_context(transcript_path: str | None) -> tuple[str, dict[str, Any]]:
    """Stripped transcript tail plus plan-mode state from raw JSONL."""
    try:
        from plan_mode import detect_plan_mode, empty_plan_mode
    except ImportError:
        empty_plan_mode = lambda: {"enabled": False, "host": "", "marker": ""}  # noqa: E731
        detect_plan_mode = lambda _p: empty_plan_mode()  # noqa: E731
    plan_mode = detect_plan_mode(transcript_path)
    if not transcript_path:
        return "", plan_mode
    try:
        from transcript_tail import TRANSCRIPT_TOKEN_BUDGET, stripped_transcript_retained
    except ImportError:
        return "", plan_mode
    # Sticky retention (not a sliding tail): keeps a byte-identical, append-only
    # prefix across consecutive same-session Stop validations so the judge prompt
    # caches instead of busting the prefix every turn (the hottest judge path).
    return stripped_transcript_retained(transcript_path, TRANSCRIPT_TOKEN_BUDGET), plan_mode


def _judge_system_with_transcript(
    base: str,
    transcript: str,
    plan_mode: dict[str, Any] | None = None,
) -> str:
    """Append plan-mode rules and session transcript tail (tail-preserving cap)."""
    base = base + _plan_mode_judge_section(plan_mode)
    if not (transcript and transcript.strip()):
        return base
    try:
        from transcript_tail import JUDGE_EFFECTIVE_MAX_CHARS, cap_judge_message
    except ImportError:
        return base
    header = base + _JUDGE_TRANSCRIPT_SECTION
    room = max(0, JUDGE_EFFECTIVE_MAX_CHARS - len(header) - 50)
    if room < 500:
        return base
    return header + cap_judge_message(transcript.strip(), room)


def _judge_user(spec: dict[str, Any], task: dict[str, Any], exit_code: int, output: str) -> str:
    payload: dict[str, Any] = {
        "goal": spec.get("restated_goal", ""),
        "task_title": task.get("title", ""),
        "check": task.get("check", ""),
        "exit_code": exit_code,
        "output": output,
    }
    # EVERY requirement already in the spec (agent + judge, all statuses, full
    # title+check) so the judge can reason about purpose overlap before adding.
    payload["current_requirements"] = _current_requirements_payload(spec)
    # Requirements the judge itself added and may now adjust (retract/revise).
    adjustable = [
        {
            "id": str(t.get("id")),
            "title": str(t.get("title") or ""),
            "check": str(t.get("check") or ""),
            "status": str(t.get("status") or ""),
        }
        for t in (spec.get("tasks") or [])
        if isinstance(t, dict) and t.get("added_by") == "judge" and t.get("status") != "retracted"
    ]
    if adjustable:
        payload["existing_judge_requirements"] = adjustable[-20:]
    kind = str(task.get("approach_kind") or "requirement")
    if kind in ("frontier", "primary"):
        payload["approach_kind"] = kind
        primary = primary_task(spec)
        if primary:
            payload["primary_approach"] = primary.get("title", "")
    return json.dumps(payload, ensure_ascii=False)


def _normalize_adjustments(raw: Any) -> list[dict[str, str]]:
    """Coerce the judge's adjust_requirements into a clean list of
    {id, action, reason[, title][, check]}, dropping malformed or no-op entries."""
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "").strip()
        action = str(item.get("action") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not tid or action not in ("retract", "revise"):
            continue
        entry: dict[str, str] = {"id": tid, "action": action, "reason": reason}
        if action == "revise":
            title = str(item.get("title") or "").strip()
            check = str(item.get("check") or "").strip()
            if title:
                entry["title"] = title
            if check:
                entry["check"] = check
            if "title" not in entry and "check" not in entry:
                continue  # a revise with nothing to change is a no-op
        out.append(entry)
    return out


def _apply_adjustments(spec: dict[str, Any], res: Any, skip_ids: Any = ()) -> list[str]:
    """Apply judge adjust_requirements: retract judge-added tasks; revise any task
    with a broken check. skip_ids blocks retract only (revise still applies)."""
    if not isinstance(res, dict):
        return []
    adjustments = _normalize_adjustments(res.get("adjust_requirements"))
    if not adjustments:
        return []
    skip = {str(s) for s in (skip_ids or ())}
    by_id = {str(t.get("id")): t for t in (spec.get("tasks") or []) if isinstance(t, dict)}
    headlines: list[str] = []
    for adj in adjustments:
        tid = adj["id"]
        t = by_id.get(tid)
        if t is None or str(t.get("status") or "") in ("retracted", "superseded"):
            continue
        reason = adj.get("reason", "")
        if adj["action"] == "retract":
            if tid in skip:
                continue
            if t.get("added_by") != "judge":
                continue
            t["status"] = "retracted"
            t["judge_reason"] = reason
            headline = f"Judge retracted {tid}: {reason[:80]}"
        else:  # revise: fix broken checks on agent or judge tasks
            if "title" in adj:
                t["title"] = adj["title"]
            if "check" in adj:
                t["check"] = adj["check"]
            t["status"] = "pending"
            t["exit"] = None
            t["output"] = ""
            t["judge_verdict"] = None
            t["judge_reason"] = reason
            t["_check_stale"] = True
            t["_revise_this_stop"] = True
            who = "Judge" if t.get("added_by") == "judge" else "Agent req"
            headline = f"{who} requirement {tid} revised: {reason[:80]}"
        notify_spec_update(spec, headline, highlight_task=tid)
        headlines.append(headline)
    return headlines


def _judge_owned_open_tasks(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Open judge-added tasks the agent cannot retract or revise."""
    out: list[dict[str, Any]] = []
    for t in spec.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        if t.get("added_by") != "judge":
            continue
        if str(t.get("status") or "") in RESOLVED_STATUSES:
            continue
        out.append(t)
    return out


def judge_heal_own_requirements(
    spec: dict[str, Any],
    *,
    transcript_path: str | None = None,
) -> list[str]:
    """Judge-only pass: revise or retract open judge-added tasks. Fail-open."""
    open_tasks = _judge_owned_open_tasks(spec)
    if not open_tasks:
        return []
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError:
        return []
    transcript, plan_mode = _judge_context(transcript_path)
    payload = {
        "goal": spec.get("restated_goal", ""),
        "current_requirements": _current_requirements_payload(spec),
        "session_context": _session_context_payload(plan_mode),
        "judge_owned_open": [
            {
                "id": str(t.get("id") or ""),
                "title": str(t.get("title") or ""),
                "check": str(t.get("check") or ""),
                "status": str(t.get("status") or ""),
                "judge_reason": str(t.get("judge_reason") or ""),
                "exit": t.get("exit"),
                "output": str(t.get("output") or "")[:1500],
                "attempts": int(t.get("attempts") or 0),
                "approach_kind": str(t.get("approach_kind") or ""),
            }
            for t in open_tasks
        ],
    }
    try:
        res = ask_structured(
            _judge_system_with_transcript(_JUDGE_HEAL_SYSTEM, transcript, plan_mode),
            json.dumps(payload, ensure_ascii=False),
            _JUDGE_HEAL_SCHEMA,
            schema_name="judge_heal",
        )
    except JudgeError:
        return []
    return _apply_adjustments(spec, res)


def _judge_system_for_task(
    task: dict[str, Any],
    *,
    transcript: str = "",
    plan_mode: dict[str, Any] | None = None,
) -> str:
    kind = str(task.get("approach_kind") or "requirement")
    if kind == "frontier":
        base = _FRONTIER_JUDGE_SYSTEM
    elif kind == "primary":
        base = _PRIMARY_JUDGE_SYSTEM
    else:
        base = _JUDGE_SYSTEM
    return _judge_system_with_transcript(base, transcript, plan_mode=plan_mode)


def _judge_schema_for_task(task: dict[str, Any]) -> dict[str, Any]:
    kind = str(task.get("approach_kind") or "requirement")
    if kind == "frontier":
        return _FRONTIER_JUDGE_SCHEMA
    return _JUDGE_SCHEMA


def _judge_result(res: Any, task: dict[str, Any] | None = None) -> tuple[int, str, list[dict[str, str]], str]:
    verdict = 1 if isinstance(res, dict) and res.get("verdict") == 1 else 0
    reason = str(res.get("reason") or "") if isinstance(res, dict) else ""
    new_reqs = _normalize_new_requirements(res.get("new_requirements")) if isinstance(res, dict) else []
    frontier_outcome = ""
    if task and str(task.get("approach_kind") or "") == "frontier" and isinstance(res, dict):
        outcome = str(res.get("outcome") or "").strip()
        if outcome in ("rejected_approach", "still_viable", "accepted_approach"):
            frontier_outcome = outcome
        verdict = 1 if outcome == "accepted_approach" else 0
    return verdict, reason, new_reqs, frontier_outcome


def _evidence_payload(evidence: dict[str, Any] | None) -> dict[str, list[str]] | None:
    """Bounded captured-activity corpus for the judge: file reads, URL fetches,
    Bash commands, and MCP tool results. This is the proof an evidence_only
    (research) requirement is adjudicated against."""
    if not isinstance(evidence, dict):
        return None

    def _take(key: str, n: int) -> list[str]:
        return [str(x) for x in (evidence.get(key) or []) if str(x)][-n:]

    out = {
        "read_paths": _take("read_paths", 30),
        "fetched_urls": _take("fetched_urls", 20),
        "ran_commands": _take("ran_commands", 20),
        "command_outputs": _take("command_outputs", 20),
        "tool_results": _take("tool_evidence", 30),
        "verifications": _take("verifications", 20),
    }
    return out if any(out.values()) else None


def _build_validate_all_user(
    spec: dict[str, Any],
    items: list[dict[str, Any]],
    plan_mode: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> str:
    """Build the unified validation payload for all open tasks."""
    tasks_payload: list[dict[str, Any]] = []
    for it in items:
        task = it["task"]
        entry: dict[str, Any] = {
            "id": str(task.get("id") or ""),
            "title": str(task.get("title") or ""),
            "check": str(task.get("check") or ""),
            "status": str(task.get("status") or ""),
            "kind": str(it.get("kind") or "validate"),
        }
        kind = str(task.get("approach_kind") or "")
        if kind:
            entry["approach_kind"] = kind
        if it.get("kind") == "dispute":
            entry["dispute_evidence"] = str(task.get("dispute_evidence") or "")
        else:
            # evidence_only: the check is prose / not a runnable command, so there
            # is no exit code to weigh. Adjudicate from the evidence corpus below.
            if it.get("evidence_only"):
                entry["evidence_only"] = True
                entry["exit_code"] = None
                entry["output"] = ""
            else:
                entry["exit_code"] = it.get("exit_code")
                entry["output"] = str(it.get("output") or "")
        tasks_payload.append(entry)
    payload: dict[str, Any] = {
        "goal": spec.get("restated_goal", ""),
        "current_requirements": _current_requirements_payload(spec),
        "tasks_to_adjudicate": tasks_payload,
        "session_context": _session_context_payload(plan_mode),
    }
    ev = _evidence_payload(evidence)
    if ev:
        payload["evidence"] = ev
    adjustable = [
        {
            "id": str(t.get("id")),
            "title": str(t.get("title") or ""),
            "check": str(t.get("check") or ""),
            "status": str(t.get("status") or ""),
        }
        for t in (spec.get("tasks") or [])
        if isinstance(t, dict) and t.get("added_by") == "judge" and t.get("status") != "retracted"
    ]
    if adjustable:
        payload["existing_judge_requirements"] = adjustable[-20:]
    primary = primary_task(spec)
    if primary:
        payload["primary_approach"] = primary.get("title", "")
    return json.dumps(payload, ensure_ascii=False)


def _validate_all_system(transcript: str, plan_mode: dict[str, Any] | None = None) -> str:
    base = _VALIDATE_ALL_SYSTEM + " " + _DISPUTE_ADJUDICATION + " " + _DISPUTE_OBSOLETE_RULE
    return _judge_system_with_transcript(base, transcript, plan_mode=plan_mode)


_COMPARISON_SYSTEM = (
    "You are a senior engineer comparing frontier approaches that were ALL explored. "
    "You receive the goal and every frontier's title, check, exit code, output, and "
    "prior judge reasoning. Your job is to select the SINGLE best frontier -- the one "
    "with the strongest empirical evidence (passing checks, better output quality, "
    "more robust approach, closer fit to the goal). You MUST select one when any "
    "frontier has accepted_approach status. Provide selection_rationale explaining "
    "WHY the winner was chosen over the others, citing specific evidence from the "
    "frontier results (exit codes, output characteristics, approach trade-offs). "
    "If NO frontier has accepted_approach status, return selected_id as null "
    "(the primary fallback will be used instead)."
)


_COMPARISON_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_id": {"type": ["string", "null"]},
        "selection_rationale": {"type": "string"},
    },
    "required": ["selected_id", "selection_rationale"],
    "additionalProperties": False,
}


def judge_frontier_comparison(spec: dict[str, Any]) -> list[str]:
    """Compare all explored frontiers and select the best. Returns headlines.

    Called after all task verdicts are applied in auto_validate_spec when
    all_frontiers_terminal(spec) is True and at least one frontier has
    accepted_approach status. Reads persisted evidence (exit, output,
    judge_reason) from each frontier task -- no new persistence layer."""
    frontiers = frontier_tasks(spec)
    accepted = [t for t in frontiers if str(t.get("status") or "") == "accepted_approach"]
    if not accepted:
        return []

    payload = {
        "goal": spec.get("restated_goal", ""),
        "frontiers": [
            {
                "id": str(t.get("id")),
                "title": str(t.get("title") or ""),
                "check": str(t.get("check") or ""),
                "exit_code": t.get("exit"),
                "output": str(t.get("output") or "")[:2000],
                "judge_reason": str(t.get("judge_reason") or ""),
                "status": str(t.get("status") or ""),
            }
            for t in frontiers
        ],
    }
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError:
        return []
    try:
        res = ask_structured(
            _COMPARISON_SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            _COMPARISON_SCHEMA,
            schema_name="frontier_comparison",
        )
    except JudgeError:
        return []

    selected_id = str(res.get("selected_id") or "").strip()
    rationale = str(res.get("selection_rationale") or "").strip()

    headlines: list[str] = []
    primary = primary_task(spec)
    for t in frontiers:
        tid = str(t.get("id"))
        if tid == selected_id:
            t["comparison_winner"] = True
            t["judge_reason"] = f"Selected as best approach: {rationale[:200]}"
            headlines.append(f"{tid} selected as best frontier: {rationale[:80]}.")
        elif str(t.get("status") or "") == "accepted_approach":
            t["status"] = "rejected_approach"
            t["comparison_winner"] = False
            headlines.append(f"{tid} not selected in comparison.")

    if primary and str(primary.get("status") or "") == "blocked":
        primary["status"] = "superseded"
        primary["judge_reason"] = f"Superseded by adopted frontier {selected_id}."
        headlines.append(f"Primary superseded by adopted frontier {selected_id}.")

    sync_heavy_phase(spec)
    advance_primary_if_ready(spec)
    return headlines


def judge_all_tasks(
    spec: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    transcript: str = "",
    plan_mode: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> list[tuple[int, str, list[dict[str, str]], str]]:
    """Judge every open task in ONE structured call from shared session context.

    Judge retract/revise headlines from adjust_requirements are stashed on
    ``spec["_stop_adjust_headlines"]`` so auto_validate_spec can merge them into
    the Stop digest -- otherwise they are applied to the spec but lost to the
    model. The stash is transient and drained + popped by auto_validate_spec; it
    avoids threading a new kwarg through the judge seam that tests stub."""
    if not items:
        return []
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError as exc:  # pragma: no cover
        return [(0, f"judge unavailable: {exc}", [], "") for _ in items]
    try:
        res = ask_structured(
            _validate_all_system(transcript, plan_mode),
            _build_validate_all_user(spec, items, plan_mode, evidence),
            _VALIDATE_ALL_SCHEMA,
            schema_name="validate_all",
        )
    except JudgeError as exc:
        return [(0, f"judge error: {exc}", [], "") for _ in items]
    raw_verdicts = res.get("task_verdicts") if isinstance(res, dict) else None
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(raw_verdicts, list):
        for v in raw_verdicts:
            if isinstance(v, dict) and v.get("id") is not None:
                by_id[str(v.get("id"))] = v
    judged_ids = {str(it["task"].get("id")) for it in items}
    out: list[tuple[int, str, list[dict[str, str]], str]] = []
    for it in items:
        tid = str(it["task"].get("id"))
        v = by_id.get(tid)
        if not v:
            out.append((0, f"judge omitted task {tid}", [], ""))
            continue
        skip = set(judged_ids)
        if it["task"].get("added_by") == "judge":
            skip.discard(tid)
        hl = _apply_adjustments(spec, v, skip_ids=skip)
        if hl:
            spec.setdefault("_stop_adjust_headlines", []).extend(hl)
        out.append(_judge_result(v, it["task"]))
    return out


def judge_task(
    spec: dict[str, Any],
    task: dict[str, Any],
    exit_code: int,
    output: str,
    *,
    transcript: str = "",
    plan_mode: dict[str, Any] | None = None,
) -> tuple[int, str, list[dict[str, str]], str]:
    """Ask the judge whether a single check output validates the task.

    Returns (verdict, reason, new_requirements, frontier_outcome).
    frontier_outcome is 'rejected_approach' or 'still_viable' for frontier tasks."""
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError as exc:  # pragma: no cover
        return 0, f"judge unavailable: {exc}", [], ""
    try:
        res = ask_structured(
            _judge_system_for_task(task, transcript=transcript, plan_mode=plan_mode),
            _judge_user(spec, task, exit_code, output),
            _judge_schema_for_task(task),
            schema_name="task_verdict",
        )
    except JudgeError as exc:
        return 0, f"judge error: {exc}", [], ""
    # skip_ids blocks retract on listed ids only; revise always allowed.
    _apply_adjustments(spec, res, skip_ids=set())
    return _judge_result(res, task)


def judge_tasks(
    spec: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    transcript: str = "",
    plan_mode: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> list[tuple[int, str, list[dict[str, str]], str]]:
    """Judge all items in one unified structured call (validate + dispute)."""
    return judge_all_tasks(spec, items, transcript=transcript, plan_mode=plan_mode, evidence=evidence)


def _normalize_scope_paths(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(p).strip() for p in raw if str(p).strip()]


def judge_discover_frontiers(
    spec: dict[str, Any],
    recent_activity: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Ask judge to propose frontier tasks from research activity. Returns added tasks."""
    if len(frontier_tasks(spec)) >= 2:
        return []
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError:
        return []
    user = json.dumps(
        {
            "goal": spec.get("restated_goal", ""),
            "existing_frontiers": [t.get("title") for t in frontier_tasks(spec)],
            "current_requirements": _current_requirements_payload(spec),
            "read_paths": (recent_activity.get("read_paths") or [])[-20:],
            "fetched_urls": (recent_activity.get("fetched_urls") or [])[-10:],
            "repo_context": spec.get("repo_context") or [],
            "prior_art": spec.get("prior_art") or [],
        },
        ensure_ascii=False,
    )
    try:
        res = ask_structured(_DISCOVER_SYSTEM, user, _DISCOVER_SCHEMA, schema_name="frontier_discover")
    except JudgeError:
        return []
    added: list[dict[str, Any]] = []
    frontiers = res.get("frontiers") if isinstance(res, dict) else []
    if not isinstance(frontiers, list):
        return []
    for item in frontiers:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        check = str(item.get("check") or "").strip()
        if not title or not check:
            continue
        if len(frontier_tasks(spec)) >= 2:
            break
        task = append_frontier_task(
            spec,
            title,
            check,
            added_by="judge",
            scope_paths=_normalize_scope_paths(item.get("scope_paths")),
        )
        reason = str(item.get("reason") or res.get("reason") or "").strip()
        if reason:
            task["discovery_reason"] = reason
        added.append(task)
    if added:
        ids = ", ".join(t["id"] for t in added)
        notify_spec_update(
            spec,
            f"Judge added frontier approach(s): {ids}. Explore these before primary.",
        )
    return added


def judge_dispute(
    spec: dict[str, Any],
    task: dict[str, Any],
    evidence: str,
    *,
    plan_mode: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Adjudicate an agent's claim that a requirement is IMPOSSIBLE.

    The agent has submitted `evidence` that the task cannot be satisfied. Return
    (verdict, reason): verdict 1 accepts the impossibility (the caller retracts the
    requirement), 0 rejects it (the requirement stays open with feedback). A judge
    failure returns (0, reason) so an unreachable judge never auto-retracts a
    requirement -- impossibility must be earned, not granted by default."""
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError as exc:  # pragma: no cover
        return 0, f"judge unavailable: {exc}"
    system = (
        "You are a strict adjudicator. An agent claims a REQUIRED task is impossible "
        "or obsolete and submits evidence. Accept (verdict 1) ONLY if the evidence genuinely "
        "proves the task cannot be done -- a real, demonstrated blocker, not a "
        "preference, a difficulty, or an excuse. Reject (verdict 0) if the evidence "
        "is weak, the task is merely hard or inconvenient, or the agent is dodging "
        "work; in reason, tell the agent bluntly what real proof would be required. "
        "Do not accept a claim that work is 'complete' here -- this is only about "
        "whether the requirement is genuinely impossible or obsolete. "
        + _JUDGE_FEEDBACK_GUIDANCE + " " + _DISPUTE_OBSOLETE_RULE
    )
    system += _plan_mode_judge_section(plan_mode)
    user = json.dumps(
        {
            "goal": spec.get("restated_goal", ""),
            "task_title": task.get("title", ""),
            "check": task.get("check", ""),
            "impossibility_evidence": evidence,
            "current_requirements": _current_requirements_payload(spec),
            "session_context": _session_context_payload(plan_mode),
        },
        ensure_ascii=False,
    )
    try:
        res = ask_structured(system, user, _DISPUTE_SCHEMA, schema_name="dispute_verdict")
    except JudgeError as exc:
        return 0, f"judge error: {exc}"
    return (
        1 if res.get("verdict") == 1 else 0,
        str(res.get("reason") or ""),
    )


def judge_hint(spec: dict[str, Any], *, signal: str, recent: str = "") -> str:
    """Proactive, verdict-free nudge for an agent that looks stuck or is wandering.

    Unlike judge_task/judge_dispute, this renders NO verdict and resolves NO task
    -- it returns advisory guidance only. Callers (the Stop completion-breaker loop
    and the PostToolUse repeated-failure loop) surface the returned string on a
    clearly-advisory channel; it can never lift a gate. Any judge failure returns
    "" so a hint never blocks and an unreachable judge is simply silent."""
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError:  # pragma: no cover
        return ""
    system = (
        "You are a calm, senior engineering lead watching an agent that appears to "
        "be stuck or making poor judgement. You are NOT judging completion, you "
        "CANNOT change any verdict, and you CANNOT lift any gate -- you only offer "
        "ONE concrete, actionable next step to get the agent unstuck. Be specific "
        "and grounded in the goal, the task board, and what the agent has been "
        "doing. If you have nothing genuinely useful to say, return an empty hint."
    )
    board = spec.get("tasks") or []
    user = json.dumps(
        {
            "goal": spec.get("restated_goal", ""),
            "why_it_looks_stuck": signal,
            "tasks": [
                {"id": t.get("id"), "title": t.get("title"), "status": t.get("status"), "judge_reason": t.get("judge_reason")}
                for t in board
                if isinstance(t, dict)
            ],
            "recent_activity": recent[:2000],
        },
        ensure_ascii=False,
    )
    try:
        res = ask_structured(system, user, _HINT_SCHEMA, schema_name="hint")
    except JudgeError:
        return ""
    return _normalize_hint(res.get("hint"))
