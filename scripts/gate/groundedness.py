#!/usr/bin/env python3
"""Overconfidence / groundedness breaker (unifable).

Two directional decisions, both by GPT-realtime-2 over merged transcript material
(host JSONL tail + prior breaker-event records + optional fresh PostToolUse output):

ARM (while disarmed). On PreToolUse the strict judge asks two questions from the
transcript: (1) did the model say something CONFIDENTLY WITHOUT BACKING IT UP, and
(2) is that assertion LOAD-BEARING for the work currently in progress (the user
request, the imminent edit/check, the decision driving the next tool)? Only when
both hold (verdict 1) does the breaker arm and block mutation tools. The arm judge
is DEBOUNCED to at most once per JUDGE_WINDOW_SECONDS (15s) per session+prompt key.
Prior DISARM/FAIL_OPEN events in the injected breaker records prevent re-arming
the same claim.

DISARM (while armed). On PostToolUse after Read/WebFetch/WebSearch/Grep/Glob/
NotebookRead, and on any PreToolUse while still armed, a claim-bound release judge
asks whether the flagged claim is grounded, retracted, or no longer load-bearing
for the work in progress. If any release condition holds, the breaker disarms.

SAFETY CAP. After BREAKER_MAX_BLOCKS consecutive blocks on one arm the breaker
fails open (disarms, logs) so a misfiring judge can never hard-lock a session.

Fails open: any judge or transcript error leaves tools unblocked.
Disable with UNIFABLE_BREAKER=0. Cap override: UNIFABLE_BREAKER_MAX_BLOCKS.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

from breaker_state import (
    adjudicated_claims,
    append_event,
    claim_already_adjudicated,
    render_events,
)
from transcript_tail import TRANSCRIPT_TOKEN_BUDGET, stripped_transcript_tail, tail_tokens

# Mutation tools the breaker can block: writes, edits, bash (both hosts: Claude
# Code Edit/Write/MultiEdit/NotebookEdit + Bash, Codex apply_patch). WebSearch,
# Read, WebFetch, Grep and Glob are NEVER in this set, so they are never blocked.
MUTATION_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "Bash"})

# PostToolUse tools that can trigger the release judge while armed.
RELEASE_TOOLS = frozenset({"Read", "WebFetch", "WebSearch", "Grep", "Glob", "NotebookRead"})

# Debounce: the ARM judge fires at most once per this many seconds per key.
JUDGE_WINDOW_SECONDS = 15

# Consecutive blocks on one arm before the breaker fails open (escape hatch).
BREAKER_MAX_BLOCKS_DEFAULT = 3

_TRANSCRIPT_TOKEN_BUDGET = TRANSCRIPT_TOKEN_BUDGET

_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "load_bearing": {
            "type": "integer",
            "enum": [0, 1],
            "description": (
                "1 only if any unproven assertion is LOAD-BEARING for the work currently in progress "
                "in the transcript -- the user's request, the file/check the model is about to mutate "
                "or run, or a fact the model must treat as settled to proceed with THAT work. 0 for "
                "narration, background explanations, speculative root-cause stories about host/tool "
                "errors the model is not using to drive the immediate action, passing asides, or "
                "claims the model retracted or labeled uncertain. When load_bearing=0, verdict MUST be 0."
            ),
        },
        "verdict": {
            "type": "integer",
            "enum": [0, 1],
            "description": (
                "1 ONLY if load_bearing=1 AND the model stated something confidently without backing "
                "it up; else 0."
            ),
        },
        "steering": {
            "type": "string",
            "description": (
                "When verdict=1, a 2-3 sentence steering prompt addressed to the model. Name the "
                "unproven claim, say its tools are restricted to read-only ones (Read, WebSearch, "
                "WebFetch, Grep, Glob) until it grounds the claim, and describe "
                "the KIND of evidence that would disarm it -- you do NOT have a repo listing, so do "
                "not invent file paths. For a claim about THIS repo's code/config, say what kind of "
                "source would settle it (the code/config that defines the behavior, or a command that "
                "proves it) and let the model find it; for a claim about EXTERNAL or platform behavior "
                "(a host feature, third-party/framework API, or library semantics), say to fetch the "
                "authoritative external documentation via web search / WebFetch -- NOT a repo file. "
                "Name a specific path only if it already appears in the transcript. Empty when verdict=0."
            ),
        },
        "claim": {
            "type": "string",
            "description": (
                "When verdict=1, the ONE specific unproven claim, in 1-2 sentences, so a later "
                "release check can decide whether THAT claim has since been grounded. Empty string "
                "when verdict=0."
            ),
        },
    },
    "required": ["verdict", "steering", "claim", "load_bearing"],
    "additionalProperties": False,
}

_DISARM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "load_bearing": {
            "type": "integer",
            "enum": [0, 1],
            "description": (
                "1 if the flagged claim is still LOAD-BEARING for the work currently in progress in "
                "the transcript (the user's request, the file being edited, the check being run). "
                "0 if the claim is narration, a retracted/corrected aside, speculative host-error "
                "storytelling not driving the immediate action, or otherwise not needed for the "
                "current work. When load_bearing=0, grounded MUST be 1 and needed MUST be empty."
            ),
        },
        "grounded": {
            "type": "integer",
            "enum": [0, 1],
            "description": (
                "1 if the breaker should release: the claim is grounded by evidence, RETRACTED, "
                "backed by a reasonable bounded search (negative/absence claims), OR load_bearing=0 "
                "(no longer blocks the current work). 0 only if load_bearing=1 AND the claim is still "
                "relied on AND genuinely unbacked. Never demand proof of a universal negative."
            ),
        },
        "needed": {
            "type": "string",
            "description": (
                "When grounded=0, a 1-2 sentence instruction addressed to the model naming EXACTLY "
                "what is still missing to disarm, matched to the claim: for a repo claim, which "
                "file(s) to read or check(s) to run; for an external/platform/API claim, the official "
                "documentation to fetch (web search / WebFetch). Empty string when grounded=1."
            ),
        },
    },
    "required": ["load_bearing", "grounded", "needed"],
    "additionalProperties": False,
}

_JUDGE_SYSTEM = (
    "You are a strict groundedness monitor watching an autonomous coding agent's recent transcript "
    "(its own statements AND the tool output it has seen). The transcript includes prior "
    "unifable_breaker gate records (event=ARM, event=DISARM, event=FAIL_OPEN) from earlier judge "
    "decisions. Do NOT arm a claim that already has event=DISARM or event=FAIL_OPEN for the same or "
    "substantially the same claim in those breaker records. "
    "Answer TWO questions in order; set load_bearing and verdict accordingly: "
    "(A) LOAD-BEARING FOR CURRENT WORK? Read the transcript for what the user asked and what the "
    "model is doing NOW (the tool about to run, the file being edited, the test being fixed). "
    "Set load_bearing=1 only if an unproven assertion, if any, would change or justify the IMMEDIATE "
    "next action on THAT work. Set load_bearing=0 when the confident-sounding text is narration, "
    "background, or explanatory speculation NOT needed for the current edit/check -- e.g. inventing "
    "a root cause for a host TaskUpdate/TaskList 'not found' or plugin-reload message while the "
    "actual work is an unrelated repo change; a post-mortem about a prior error; status commentary; "
    "or a hypothesis the model marks as uncertain or retracts. If the model says the aside is not "
    "load-bearing or retracts the claim, load_bearing=0. "
    "(B) UNGROUNDED CONFIDENT ASSERTION? Only if load_bearing=1, ask whether the model asserted a "
    "root cause, fix, or fact as SETTLED without reading the source, running the check, or citing "
    "evidence (especially about an API, config, or file it never actually read). A normal hypothesis "
    "the model is about to test is NOT a violation. Use tool output already in the transcript: if "
    "evidence for the claim is present, verdict=0. "
    "MATCH the grounding source to the claim's nature. A claim about THIS repo's code or config is "
    "grounded by reading the repo source or running the check. A claim about EXTERNAL or platform "
    "behavior -- how a host feature works (slash commands, hooks, skills, Task tools), a third-party "
    "or framework API, or library semantics -- is grounded by AUTHORITATIVE EXTERNAL DOCUMENTATION the "
    "model fetches via web search / WebFetch; for such a claim the correct steering points at the "
    "official documentation to fetch, NOT at a repo file like AGENTS.md. Never demand repo-internal "
    "evidence for a claim whose truth lives in external docs. You do NOT have a repo file listing -- "
    "describe the KIND of source that would settle the claim; name a path only if it already appears "
    "in the transcript. Judge whether what the model read or fetched SUPPORTS the claim. "
    "ARM ONLY when load_bearing=1 AND the assertion is genuinely ungrounded: verdict=1, name the "
    "claim, write a 2-3 sentence steering prompt telling the model its tools are restricted to "
    "read-only ones (Read, WebSearch, WebFetch, Grep, Glob) until it grounds THAT claim. "
    "Otherwise verdict=0, load_bearing=0 or 1 as appropriate, steering MUST be empty string, claim "
    "MUST be empty. Call the function exactly once."
)

_DISARM_SYSTEM = (
    "You are a groundedness RELEASE monitor for an autonomous coding agent. The agent was earlier "
    "flagged for ONE confident, unproven claim, given to you below. Look at what the agent has "
    "since done in the transcript -- reads, checks, retractions, and the work currently in progress "
    "(user request, file being edited, tool about to run), including any FRESH TOOL OUTPUT block. "
    "Answer TWO questions; set load_bearing and grounded accordingly: "
    "(A) IS THE FLAGGED CLAIM STILL LOAD-BEARING FOR CURRENT WORK? Set load_bearing=0 when the "
    "claim is narration, a retracted/corrected aside, speculative root-cause storytelling about "
    "host/tool errors (TaskUpdate 'not found', plugin reload) that the model is NOT using to drive "
    "the immediate repo edit/check, or otherwise not needed for the work NOW in the transcript. "
    "Set load_bearing=1 only if the model still relies on that claim for the immediate next action. "
    "(B) SHOULD THE BREAKER RELEASE? Set grounded=1 if ANY hold: (1) load_bearing=0 -- release "
    "without requiring further evidence; (2) the claim was RETRACTED or corrected; (3) the model "
    "read the source / ran the check / cited file:line or command output that backs the claim; "
    "(4) negative/absence claim backed by a reasonable bounded search; (5) external/platform claim "
    "backed by fetched authoritative documentation. When load_bearing=0, grounded MUST be 1. "
    "Set grounded=0 ONLY when load_bearing=1 AND the claim is still relied on AND genuinely "
    "unbacked; then write `needed` naming exactly what is still missing. When grounded=1, needed "
    "MUST be empty. Judge only the named claim. Call the function once."
)


def enabled() -> bool:
    return os.environ.get("UNIFABLE_BREAKER", "1").strip().lower() not in ("0", "false", "no", "off")


def max_blocks() -> int:
    try:
        return max(1, int(os.environ.get("UNIFABLE_BREAKER_MAX_BLOCKS", BREAKER_MAX_BLOCKS_DEFAULT)))
    except (TypeError, ValueError):
        return BREAKER_MAX_BLOCKS_DEFAULT


def is_mutation_tool(tool_name: str) -> bool:
    return tool_name in MUTATION_TOOLS


def is_release_tool(tool_name: str) -> bool:
    return tool_name in RELEASE_TOOLS


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("/", "-").replace("_", "-")


def locate_transcript(input_data: dict) -> str | None:
    tp = input_data.get("transcript_path")
    if tp and Path(str(tp)).is_file():
        return str(tp)
    sid = input_data.get("session_id")
    cwd = input_data.get("cwd") or os.getcwd()
    if sid:
        cand = Path.home() / ".claude" / "projects" / _encode_cwd(str(cwd)) / f"{sid}.jsonl"
        if cand.is_file():
            return str(cand)
    return None


def transcript_segment(input_data: dict, max_tokens: int = _TRANSCRIPT_TOKEN_BUDGET) -> str:
    path = locate_transcript(input_data)
    if not path:
        return ""
    return stripped_transcript_tail(path, max_tokens)


def judge_transcript(
    input_data: dict,
    events: list[dict[str, Any]],
    *,
    fresh_tool: str | None = None,
    max_tokens: int = _TRANSCRIPT_TOKEN_BUDGET,
) -> str:
    """Merged judge input: breaker events + host transcript tail + optional fresh tool block."""
    parts: list[str] = []
    rendered = render_events(events)
    if rendered:
        parts.append(rendered.rstrip())
    host = transcript_segment(input_data, max_tokens=max_tokens)
    if host:
        parts.append(host.rstrip())
    if fresh_tool and fresh_tool.strip():
        parts.append(
            '<record line="000000" type="fresh_tool" role="tool">\n'
            + fresh_tool.strip()
            + "\n</record>"
        )
    if not parts:
        return ""
    return tail_tokens("\n\n".join(parts), max_tokens)


JudgeFn = Callable[[str, str, dict], dict]


def _default_judge(system: str, user: str, schema: dict) -> dict:
    from codex_judge import ask_structured

    return ask_structured(system, user, schema, schema_name="groundedness")


def judge_segment(segment: str, judge: JudgeFn | None = None) -> tuple[int, str]:
    verdict, steering, _claim = arm_judge(segment, events=[], judge=judge)
    return verdict, steering


def arm_judge(
    segment: str,
    events: list[dict[str, Any]] | None = None,
    judge: JudgeFn | None = None,
) -> tuple[int, str, str]:
    if not segment.strip():
        return 0, "", ""
    fn = judge or _default_judge
    system = _JUDGE_SYSTEM
    done = adjudicated_claims(events or [])
    if done:
        claims_str = "\n".join(f"- {c}" for c in done)
        system += (
            f"\n\nDo NOT flag any of the following claims as they have already been "
            f"adjudicated or grounded:\n{claims_str}"
        )
    obj = fn(system, segment, _JUDGE_SCHEMA)
    load_bearing = int(obj.get("load_bearing", 0) or 0) == 1
    verdict = 1 if int(obj.get("verdict", 0) or 0) == 1 else 0
    if verdict == 1 and not load_bearing:
        verdict = 0
    steering = str(obj.get("steering", "") or "") if verdict == 1 else ""
    claim = str(obj.get("claim", "") or "") if verdict == 1 else ""
    return verdict, steering, claim


def disarm_judge(claim: str, segment: str, judge: JudgeFn | None = None) -> tuple[bool, str]:
    if not segment.strip():
        return False, ""
    fn = judge or _default_judge
    user = f"FLAGGED CLAIM:\n{claim}\n\nTRANSCRIPT (what the model has since read/run/cited):\n{segment}"
    obj = fn(_DISARM_SYSTEM, user, _DISARM_SCHEMA)
    load_bearing = int(obj.get("load_bearing", 1) or 0) == 1
    grounded = int(obj.get("grounded", 0) or 0) == 1
    if not load_bearing:
        grounded = True
    needed = str(obj.get("needed", "") or "") if not grounded else ""
    return grounded, needed


def _apply_release(
    state: dict,
    claim: str,
    grounded: bool,
    needed: str,
) -> bool:
    """Record release outcome on `state`. Returns True if disarmed."""
    if grounded:
        append_event(state, "DISARM", claim=claim, grounded=True)
        disarm(state)
        return True
    if needed:
        append_event(state, "NEEDED", claim=claim, needed=needed)
        state["breaker_steering"] = needed
    return False


def breaker_key(session_id: str, active_task: str) -> str:
    return f"{session_id or 'no-session'}|{active_task or ''}"


def should_judge(state: dict, key: str, now: float, window: float = JUDGE_WINDOW_SECONDS) -> bool:
    if state.get("breaker_key") != key:
        return True
    last = state.get("breaker_judged_at") or 0.0
    try:
        return (now - float(last)) >= window
    except (TypeError, ValueError):
        return True


def arm(state: dict, key: str, now: float, steering: str, claim: str) -> None:
    state["breaker_key"] = key
    state["breaker_judged_at"] = now
    state["breaker_armed"] = True
    state["breaker_steering"] = steering
    state["breaker_claim"] = claim
    state["breaker_armed_at"] = now
    state["breaker_block_count"] = 0
    append_event(state, "ARM", claim=claim, steering=steering)


def disarm(state: dict) -> None:
    state["breaker_armed"] = False
    state["breaker_steering"] = ""
    state["breaker_claim"] = ""
    state["breaker_armed_at"] = 0.0
    state["breaker_block_count"] = 0


def record_verdict(state: dict, key: str, now: float, verdict: int, steering: str, claim: str = "") -> None:
    if verdict == 1:
        arm(state, key, now, steering, claim)
        return
    disarm(state)
    state["breaker_key"] = key
    state["breaker_judged_at"] = now


def _release_log(count: int) -> None:
    try:
        sys.stderr.write(
            f"[unifable breaker] auto-released after {count} consecutive blocks (fail-open)\n"
        )
    except Exception:
        pass


def evaluate_pre_tool(
    input_data: dict,
    state: dict,
    now: float,
    active_task: str,
    judge: JudgeFn | None = None,
) -> tuple[bool, str]:
    """PreToolUse path: arm judge (debounced) and block mutation tools while armed."""
    if not enabled():
        return False, ""
    tool = str(input_data.get("tool_name") or "")
    key = breaker_key(str(input_data.get("session_id") or ""), str(active_task or ""))
    events = state.get("events") if isinstance(state.get("events"), list) else []
    try:
        armed = bool(state.get("breaker_armed"))
        if armed and state.get("breaker_key") != key:
            append_event(state, "STALE_ARM_DROPPED", claim=str(state.get("breaker_claim") or ""))
            disarm(state)
            armed = False
        if not armed and should_judge(state, key, now):
            segment = judge_transcript(input_data, events)
            verdict, steering, claim = arm_judge(segment, events=events, judge=judge)
            if verdict == 1 and claim and claim_already_adjudicated(claim, events):
                verdict, steering, claim = 0, "", ""
            record_verdict(state, key, now, verdict, steering, claim)
        elif armed:
            claim = str(state.get("breaker_claim") or "")
            if claim:
                segment = judge_transcript(input_data, events)
                _apply_release(state, claim, *disarm_judge(claim, segment, judge=judge))
    except Exception:
        return False, ""
    if is_mutation_tool(tool) and state.get("breaker_armed"):
        count = int(state.get("breaker_block_count") or 0) + 1
        state["breaker_block_count"] = count
        if count >= max_blocks():
            _release_log(count)
            claim = str(state.get("breaker_claim") or "")
            append_event(state, "FAIL_OPEN", claim=claim, block_count=count)
            disarm(state)
            return False, ""
        return True, str(state.get("breaker_steering") or "")
    return False, ""


def evaluate_post_tool_release(
    input_data: dict,
    state: dict,
    fresh_tool: str,
    judge: JudgeFn | None = None,
) -> tuple[bool, str, str]:
    """PostToolUse release path. Returns (grounded, needed, context_message)."""
    if not enabled():
        return False, "", ""
    if not state.get("breaker_armed"):
        return False, "", ""
    tool = str(input_data.get("tool_name") or "")
    if not is_release_tool(tool):
        return False, "", ""
    claim = str(state.get("breaker_claim") or "")
    if not claim:
        return False, "", ""
    events = state.get("events") if isinstance(state.get("events"), list) else []
    try:
        segment = judge_transcript(input_data, events, fresh_tool=fresh_tool)
        grounded, needed = disarm_judge(claim, segment, judge=judge)
        if _apply_release(state, claim, grounded, needed):
            return True, "", (
                "unifable breaker open: the flagged claim is grounded. "
                "Write/Edit/Bash are unrestricted again."
            )
        if needed:
            return False, needed, f"unifable breaker: still armed. {needed}"
    except Exception:
        return False, "", ""
    return False, "", ""


# Backward-compatible alias for tests migrating from evaluate().
evaluate = evaluate_pre_tool
