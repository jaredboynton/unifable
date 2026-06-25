#!/usr/bin/env python3
"""Groundedness-breaker judges: arm / disarm / monitor structured calls plus the
predicate self-verify and stepwise-director field parsing.

Extracted from groundedness.py; re-exported by the groundedness facade. Imports
from breaker_filters / breaker_prompts / breaker_runtime (all downward).
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

try:
    from breaker_filters import (
        _claim_supported_by_spec_board,
        claim_describes_loaded_skill,
        is_harness_self_referential,
        is_task_board_status_claim,
        should_suppress_path_hypothesis_arm,
    )
    from breaker_prompts import (
        _DISARM_SCHEMA,
        _DISARM_SYSTEM,
        _JUDGE_SCHEMA,
        _JUDGE_SYSTEM,
        _MONITOR_SCHEMA,
        _MONITOR_SYSTEM,
    )
    from breaker_runtime import DIRECTIVE_MAX_CHARS
    from breaker_state import adjudicated_claims
    from transcript_tail import (
        JUDGE_EFFECTIVE_MAX_CHARS,
        cap_judge_message,
        fit_judge_user_message,
    )
except ImportError:  # pragma: no cover
    from scripts.gate.breaker_filters import (
        _claim_supported_by_spec_board,
        claim_describes_loaded_skill,
        is_harness_self_referential,
        is_task_board_status_claim,
        should_suppress_path_hypothesis_arm,
    )
    from scripts.gate.breaker_prompts import (
        _DISARM_SCHEMA,
        _DISARM_SYSTEM,
        _JUDGE_SCHEMA,
        _JUDGE_SYSTEM,
        _MONITOR_SCHEMA,
        _MONITOR_SYSTEM,
    )
    from scripts.gate.breaker_runtime import DIRECTIVE_MAX_CHARS
    from scripts.gate.breaker_state import adjudicated_claims
    from scripts.gate.transcript_tail import (
        JUDGE_EFFECTIVE_MAX_CHARS,
        cap_judge_message,
        fit_judge_user_message,
    )


JudgeFn = Callable[[str, str, dict], dict]


@dataclass(frozen=True)
class ReleaseVerdict:
    grounded: bool
    needed: str
    load_bearing: bool
    provisional: bool
    lift_reason: str
    lift_scope: str


def _default_judge(system: str, user: str, schema: dict) -> dict:
    from judge_transport import ask_structured

    return ask_structured(system, user, schema, schema_name="groundedness")


def judge_segment(segment: str, judge: JudgeFn | None = None) -> tuple[int, str]:
    verdict, steering, _claim = arm_judge(segment, events=[], judge=judge)
    return verdict, steering


_VERIFY_MAX_ENTRIES = 20


_VERIFY_MAX_BYTES = 2_000_000


def _verify_read(cwd: str, rel: str) -> str | None:
    """Read a repo file for predicate checking. None when the path escapes cwd, is
    missing, oversized, or unreadable -- callers treat None as unverifiable."""
    try:
        from pathlib import Path

        base = Path(cwd or ".").resolve()
        target = Path(rel)
        target = target.resolve() if target.is_absolute() else (base / target).resolve()
        if target != base and base not in target.parents:
            return None  # containment: never read outside cwd
        if not target.is_file():
            return None
        if target.stat().st_size > _VERIFY_MAX_BYTES:
            return None
        return target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def verify_claim_predicate(predicate: Any, cwd: str) -> str:
    """Return 'confirmed' | 'refuted' | 'unverifiable' for a judge-supplied predicate.

    predicate = {must_contain: [{file, text}], must_not_contain: [{file, text}]}.
    confirmed: every must_contain text is present AND every must_not_contain text is
    absent in its file. refuted: the files contradict the claim. unverifiable: empty
    or malformed predicate, a missing/oversized/escaping file, or any error."""
    try:
        if not isinstance(predicate, dict):
            return "unverifiable"
        contains = predicate.get("must_contain") or []
        forbids = predicate.get("must_not_contain") or []
        if not isinstance(contains, list) or not isinstance(forbids, list):
            return "unverifiable"
        entries = [e for e in (list(contains) + list(forbids)) if isinstance(e, dict)]
        if not entries or len(entries) > _VERIFY_MAX_ENTRIES:
            return "unverifiable"
        cache: dict[str, str | None] = {}

        def body(rel: str) -> str | None:
            if rel not in cache:
                cache[rel] = _verify_read(cwd, rel)
            return cache[rel]

        for entry in contains:
            f = str(entry.get("file") or "").strip()
            text = str(entry.get("text") or "")
            if not f or text == "":
                return "unverifiable"
            content = body(f)
            if content is None:
                return "unverifiable"
            if text not in content:
                return "refuted"
        for entry in forbids:
            f = str(entry.get("file") or "").strip()
            text = str(entry.get("text") or "")
            if not f or text == "":
                return "unverifiable"
            content = body(f)
            if content is None:
                return "unverifiable"
            if text in content:
                return "refuted"
        return "confirmed"
    except Exception:
        return "unverifiable"


def _parse_director_fields(obj: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract the stepwise director's (directive, tool_scope) from a judge object.

    Token-aware: the directive is truncated to DIRECTIVE_MAX_CHARS. The tool_scope
    is normalized to {allow: [str], deny: [str]} (anything malformed -> empty), and
    the directive is folded in as scope['directive'] so tool_scope.in_scope can
    surface it as the block reason. Fail-safe: any error yields ('', {})."""
    try:
        directive = str(obj.get("directive") or "").strip()
        if len(directive) > DIRECTIVE_MAX_CHARS:
            directive = directive[: DIRECTIVE_MAX_CHARS - 3].rstrip() + "..."
        raw = obj.get("tool_scope")
        scope: dict[str, Any] = {}
        if isinstance(raw, dict):
            allow = [t for t in (raw.get("allow") or []) if isinstance(t, str)]
            deny = [t for t in (raw.get("deny") or []) if isinstance(t, str)]
            if allow:
                scope["allow"] = allow
            if deny:
                scope["deny"] = deny
        if scope and directive:
            scope["directive"] = directive
        return directive, scope
    except Exception:
        return "", {}


def arm_judge(
    segment: str,
    events: list[dict[str, Any]] | None = None,
    judge: JudgeFn | None = None,
    input_data: dict | None = None,
    out: dict[str, Any] | None = None,
) -> tuple[int, str, str]:
    if not segment.strip():
        return 0, "", ""
    fn = judge or _default_judge
    # The system prompt MUST stay byte-identical across calls so it forms a stable,
    # cacheable prefix (gpt-realtime-2 prompt caching is prefix-hash based). The
    # adjudicated-claims list is volatile (it grows as claims are released), so it
    # rides the END of the user message -- after the append-only transcript -- where
    # it cannot shift the cached prefix. See docs/evidence-gate-design.md.
    user = segment
    done = adjudicated_claims(events or [])
    if done:
        claims_str = "\n".join(f"- {c}" for c in done)
        append = (
            f"\n\nALREADY ADJUDICATED -- do NOT flag any of the following claims; they "
            f"have already been grounded or released:\n{claims_str}"
        )
        room = JUDGE_EFFECTIVE_MAX_CHARS - len(_JUDGE_SYSTEM) - len(segment)
        if room > 0:
            user = segment + cap_judge_message(append, room)
    obj = fn(_JUDGE_SYSTEM, user, _JUDGE_SCHEMA)
    # Stepwise director: capture the directive + tool_scope from the SAME judge
    # object, independent of the arm verdict and its suppressions below.
    if out is not None:
        directive, scope = _parse_director_fields(obj)
        out["directive"] = directive
        out["tool_scope"] = scope
    load_bearing = int(obj.get("load_bearing", 0) or 0) == 1
    verdict = 1 if int(obj.get("verdict", 0) or 0) == 1 else 0
    if verdict == 1 and not load_bearing:
        verdict = 0
    steering = str(obj.get("steering", "") or "") if verdict == 1 else ""
    claim = str(obj.get("claim", "") or "") if verdict == 1 else ""
    if verdict == 1 and _claim_supported_by_spec_board(claim, segment):
        return 0, "", ""
    if verdict == 1 and (
        is_harness_self_referential(claim) or is_harness_self_referential(steering) or is_task_board_status_claim(claim)
    ):
        return 0, "", ""
    if verdict == 1 and claim_describes_loaded_skill(claim, segment):
        return 0, "", ""
    if verdict == 1 and should_suppress_path_hypothesis_arm(claim, segment, input_data):
        return 0, "", ""
    # Predicate self-verify (de-escalation only): if the judge supplied a falsifiable
    # predicate that the repo files CONFIRM, the claim is already true -- do not arm.
    # Refuted/unverifiable leaves the verdict unchanged, so this can only remove a
    # false arm, never add a block.
    if verdict == 1:
        cwd = str((input_data or {}).get("cwd") or os.getcwd())
        if verify_claim_predicate(obj.get("verify"), cwd) == "confirmed":
            return 0, "", ""
    return verdict, steering, claim


def disarm_judge(
    claim: str,
    segment: str,
    *,
    user_goal: str = "",
    judge: JudgeFn | None = None,
) -> ReleaseVerdict:
    if not segment.strip():
        return ReleaseVerdict(False, "", True, False, "", "")
    if _claim_supported_by_spec_board(claim, segment):
        return ReleaseVerdict(True, "", False, False, "", "")
    if is_harness_self_referential(claim):
        return ReleaseVerdict(True, "", False, False, "", "")
    if claim_describes_loaded_skill(claim, segment):
        return ReleaseVerdict(True, "", False, False, "", "")
    fn = judge or _default_judge
    goal_block = f"USER GOAL:\n{user_goal}\n\n" if user_goal else ""
    prefix = f"{goal_block}FLAGGED CLAIM:\n{claim}\n\nTRANSCRIPT (what the model has since read/run/cited):\n"
    user = fit_judge_user_message(prefix, segment)
    obj = fn(_DISARM_SYSTEM, user, _DISARM_SCHEMA)
    load_bearing = int(obj.get("load_bearing", 1) or 0) == 1
    grounded = int(obj.get("grounded", 0) or 0) == 1
    if not load_bearing:
        grounded = True
    provisional = int(obj.get("provisional_release", 0) or 0) == 1
    if grounded or not load_bearing:
        provisional = False
    lift_reason = str(obj.get("lift_reason", "") or "") if provisional else ""
    lift_scope = str(obj.get("lift_scope", "") or "") if provisional else ""
    needed = str(obj.get("needed", "") or "") if not grounded and not provisional else ""
    return ReleaseVerdict(grounded, needed, load_bearing, provisional, lift_reason, lift_scope)


def monitor_provisional_judge(
    claim: str,
    scope: str,
    segment: str,
    tool_name: str,
    *,
    user_goal: str = "",
    judge: JudgeFn | None = None,
) -> tuple[int, str, str]:
    """Returns (drift_level, feedback). drift_level 0=on track, 1=advisory, 2=re-arm."""
    if not segment.strip():
        return 0, ""
    fn = judge or _default_judge
    goal_block = f"USER GOAL:\n{user_goal}\n\n" if user_goal else ""
    prefix = f"{goal_block}FLAGGED CLAIM:\n{claim}\n\nLIFT SCOPE:\n{scope}\n\nIMMINENT TOOL:\n{tool_name}\n\nTRANSCRIPT:\n"
    user = fit_judge_user_message(prefix, segment)
    obj = fn(_MONITOR_SYSTEM, user, _MONITOR_SCHEMA)
    drift = int(obj.get("drift_level", 0) or 0)
    if drift not in (0, 1, 2):
        drift = 0
    feedback = str(obj.get("feedback", "") or "").strip() if drift in (1, 2) else ""
    return drift, feedback
