#!/usr/bin/env python3
"""Groundedness-breaker judges: arm / disarm / monitor structured calls plus the
predicate self-verify and stepwise-director field parsing.

Extracted from groundedness.py; re-exported by the groundedness facade. Imports
from breaker_filters / breaker_prompts / breaker_runtime (all downward).
"""
from __future__ import annotations

import os
import re
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
    from file_refs import build_file_index, rehydrate_file_refs
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
    from scripts.gate.file_refs import build_file_index, rehydrate_file_refs
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


_SELF_RESOLVE_TIMEOUT = 6.0
_SELF_RESOLVE_MAX_CHARS = 8000


def _self_resolve_enabled() -> bool:
    """Read-only self-resolution is on by default; UNIFABLE_BREAKER_SELF_RESOLVE=0 disables it."""
    return os.environ.get("UNIFABLE_BREAKER_SELF_RESOLVE", "1").strip().lower() not in ("0", "false", "no", "off")


def run_explore_search(
    query: str,
    cwd: str,
    *,
    timeout: float = _SELF_RESOLVE_TIMEOUT,
    max_chars: int = _SELF_RESOLVE_MAX_CHARS,
) -> str:
    """Run the unitrace skill's search.sh --json READ-ONLY and return a compact
    snippets blob (capped), or '' on any failure/timeout/missing script. Never
    raises -- breaker self-resolution is fail-open."""
    q = str(query or "").strip()
    if not q:
        return ""
    try:
        try:
            from research_bash_guidance import resolve_explore_search_sh
        except ImportError:  # pragma: no cover
            from scripts.gate.research_bash_guidance import resolve_explore_search_sh
        script = resolve_explore_search_sh()
        if not script:
            return ""
        import subprocess

        proc = subprocess.run(
            ["bash", str(script), "--json", "--root", str(cwd or "."), q],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (proc.stdout or "").strip()
        return out[:max_chars] if out else ""
    except Exception:
        return ""


def _self_resolve_via_search(
    claim: str,
    resolve_query: Any,
    segment: str,
    cwd: str,
    *,
    user_goal: str = "",
) -> bool:
    """Read-only self-resolution: gather repo evidence via unitrace search and ask the
    release judge whether the flagged claim is now grounded. Returns True to
    DE-ESCALATE (do not arm). Fail-open: disabled, empty query, no search results,
    or any error returns False so the arm verdict stands -- this can only REMOVE a
    false arm, never add a block."""
    if not _self_resolve_enabled():
        return False
    q = str(resolve_query or "").strip()
    if not q:
        return False
    try:
        snippets = run_explore_search(q, cwd)
        if not snippets:
            return False
        enriched = f"{segment}\n\n[breaker self-resolution] read-only unitrace search for '{q}' returned:\n{snippets}"
        verdict = disarm_judge(claim, enriched, user_goal=user_goal)
        return bool(getattr(verdict, "grounded", False))
    except Exception:
        return False


def _self_resolve_via_command(
    claim: str,
    verify_cmd: Any,
    segment: str,
    cwd: str,
    *,
    user_goal: str = "",
) -> bool:
    """Read-only self-resolution via a gpt-realtime-2-AUTHORED command run on the
    recon/exec lane. gpt-realtime-2 authors the command; the host gates it through
    the read-only allowlist and runs it; the captured (exit, output) are fed back
    to the release judge, which decides. mini/the lane contribute ZERO judgment.

    Returns True to DE-ESCALATE (do not arm). Fail-open: disabled, empty command,
    a non-read-only command (never runs), a non-zero exit, or any error returns
    False so the arm verdict stands -- this can only REMOVE a false arm, never add
    a block."""
    if not _self_resolve_enabled():
        return False
    cmd = str(verify_cmd or "").strip()
    if not cmd:
        return False
    try:
        from recon_lane import run_validation_command

        res = run_validation_command(cmd, cwd)
        # Deterministic gate: the command must have actually run read-only AND exited
        # 0. A blocked/mutating command or a non-zero exit leaves the arm intact.
        if not res.get("ran") or res.get("exit_code") != 0:
            return False
        out = str(res.get("output") or "")
        enriched = (
            f"{segment}\n\n[breaker self-resolution] read-only command `{cmd}` exited 0; output:\n{out}"
        )
        verdict = disarm_judge(claim, enriched, user_goal=user_goal)
        return bool(getattr(verdict, "grounded", False))
    except Exception:
        return False


def _sanction_verify_tasks(raw: Any, cwd: str) -> list[dict[str, str]]:
    """Sanction judge-supplied verify_tasks via the verify lane. Fail-open to []."""
    try:
        try:
            from verify_lane import sanction_tasks
        except ImportError:  # pragma: no cover
            from scripts.gate.verify_lane import sanction_tasks
        return sanction_tasks(raw, cwd)
    except Exception:
        return []


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


def _director_state_block(state: dict[str, Any] | None, input_data: dict | None) -> str:
    """Render the director's OWN recent turns + the tool the agent is now attempting.

    The director judge is a stateless one-shot, so without this it cannot see what it
    previously told the model and re-paraphrases the same block forever. This block
    gives it that memory plus the imminent tool, so it can RELEASE (advance the step,
    move a now-needed tool from deny to allow) once the transcript shows the step was
    done. Fail-safe: any error yields '' (no block, judge behaves as before)."""
    try:
        if not isinstance(state, dict):
            return ""
        history = state.get("breaker_directive_history")
        if not isinstance(history, list) or not history:
            return ""
        tool = str((input_data or {}).get("tool_name") or "").strip()
        lines: list[str] = [
            "DIRECTOR STATE -- your OWN prior directives this session (most recent last). "
            "If the transcript above shows the agent ALREADY performed the latest directive's "
            "action, you MUST advance: issue the next step and OPEN the scope (move the "
            "now-needed tool from deny to allow). Do NOT re-issue a paraphrase of an "
            "already-satisfied directive, and never deny the only tool that could carry it out.",
        ]
        for h in history[-6:]:
            if not isinstance(h, dict):
                continue
            d = str(h.get("directive") or "").strip()
            deny = [t for t in (h.get("deny") or []) if isinstance(t, str)]
            allow = [t for t in (h.get("allow") or []) if isinstance(t, str)]
            scope_bits = []
            if allow:
                scope_bits.append("allow=" + ",".join(allow))
            if deny:
                scope_bits.append("deny=" + ",".join(deny))
            scope_str = (" [" + " ".join(scope_bits) + "]") if scope_bits else ""
            lines.append(f"- {d}{scope_str}")
        if tool:
            blocked = ""
            scope = state.get("breaker_tool_scope")
            if isinstance(scope, dict):
                deny = {t for t in (scope.get("deny") or []) if isinstance(t, str)}
                allow = {t for t in (scope.get("allow") or []) if isinstance(t, str)}
                if tool in deny or (allow and tool not in allow):
                    blocked = " (currently BLOCKED by your scope)"
            lines.append(f"IMMINENT TOOL the agent is attempting: {tool}{blocked}")
        return "\n".join(lines)
    except Exception:
        return ""


def arm_judge(
    segment: str,
    events: list[dict[str, Any]] | None = None,
    judge: JudgeFn | None = None,
    input_data: dict | None = None,
    out: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
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
    # FILE INDEX (pointer + rehydrate): hand the judge a numbered list of the paths
    # it already saw and have it reference files by [[n]] instead of retyping long
    # names (which it truncates). The host rehydrates the pointers below, so a file
    # reference is lossless by construction. Mirrors the explore READ INDEX pattern.
    index_text, ordered_paths = build_file_index(segment)
    appended = f"\n\n{index_text}" if index_text else ""
    done = adjudicated_claims(events or [])
    if done:
        claims_str = "\n".join(f"- {c}" for c in done)
        appended += (
            f"\n\nALREADY ADJUDICATED -- do NOT flag any of the following claims; they "
            f"have already been grounded or released:\n{claims_str}"
        )
    director_state = _director_state_block(state, input_data)
    appended += ("\n\n" + director_state) if director_state else ""
    if appended:
        room = JUDGE_EFFECTIVE_MAX_CHARS - len(_JUDGE_SYSTEM) - len(segment)
        if room > 0:
            user = segment + cap_judge_message(appended, room)
    obj = fn(_JUDGE_SYSTEM, user, _JUDGE_SCHEMA)
    # Rehydrate [[n]] file-pointers the judge emitted back to exact paths, before any
    # field is read by the director parse or the steering/claim extraction below.
    if ordered_paths and isinstance(obj, dict):
        for _field in ("directive", "steering"):
            _val = obj.get(_field)
            if isinstance(_val, str) and _val:
                obj[_field] = rehydrate_file_refs(_val, ordered_paths)
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
        # Read-only self-resolution: if the judge named a search that would settle the
        # claim, run it (unitrace search.sh, read-only) and de-escalate if the gathered
        # evidence grounds it -- instead of arming and forcing the model to re-read what
        # the breaker could check here. De-escalate-only; disabled/empty/timeout/error
        # leaves the arm verdict intact, so it can never add a block.
        if _self_resolve_via_search(claim, obj.get("resolve_query"), segment, cwd):
            return 0, "", ""
        # Read-only command self-resolution: if the judge authored a single read-only
        # command whose exit code settles the claim, the recon/exec lane gates it
        # (read-only allowlist) and runs it; on exit 0 + grounded release the arm
        # de-escalates -- instead of forcing the model to re-run what the breaker can
        # run here. De-escalate-only: a blocked/mutating command, a non-zero exit, or
        # any error leaves the arm verdict intact, so it can never add a block.
        if _self_resolve_via_command(claim, obj.get("verify_cmd"), segment, cwd):
            return 0, "", ""
        # Auto-grounding (async): the read-only lanes could not settle this claim, but
        # the judge may have decomposed it into atomic verification tasks whose
        # repo-sanctioned commands the breaker can RUN in the background. Sanction them
        # here and hand them to the orchestrator (via `out`) to dispatch -- arm_judge
        # stays decision-only; the subprocess side effect lives in orchestration. The
        # arm verdict STANDS (the first mutation still blocks until results land).
        if out is not None:
            out["verify_tasks"] = _sanction_verify_tasks(obj.get("verify_tasks"), cwd)
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


_PHANTOM_TASK_ID_RE = re.compile(r"\bT\d+\b")
_MONITOR_GENERIC_FEEDBACK = "Return to the verification scope."


def _scrub_phantom_task_ids(feedback: str, segment: str) -> str:
    """Drop T<n> task-ID citations the monitor invented: any T\\d+ token not present
    verbatim in the segment (which includes the rendered spec board) is removed, so a
    hallucinated 'T17/T18 unresolved' can never reach the model. Scrubs the MESSAGE
    only; the drift verdict is unchanged. Fail-safe: any error returns feedback as-is."""
    try:
        text = str(feedback or "")
        if not text.strip():
            return text
        phantom = {tok for tok in _PHANTOM_TASK_ID_RE.findall(text) if tok not in segment}
        if not phantom:
            return text
        # Remove each phantom token together with adjacent list punctuation/space
        # (covers "T17/T18", "T17, T18", " T17 ").
        cleaned = re.sub(r"[\s,/]*\b(?:" + "|".join(re.escape(t) for t in phantom) + r")\b", "", text)
        cleaned = re.sub(r"\(\s*\)", "", cleaned)
        cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,/;:-")
        return cleaned if cleaned.strip() else _MONITOR_GENERIC_FEEDBACK
    except Exception:
        return feedback


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
    if feedback:
        feedback = _scrub_phantom_task_ids(feedback, segment)
    return drift, feedback
