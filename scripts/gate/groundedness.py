#!/usr/bin/env python3
"""Overconfidence / groundedness breaker (unifable).

Two directional decisions, both by GPT-realtime-2 over the recent transcript:

ARM (while disarmed). On PreToolUse the strict judge asks: did the model say
something CONFIDENTLY WITHOUT BACKING IT UP? If yes (verdict 1) it returns a
steering prompt AND names the specific claim; the breaker ARMS a block on mutation
tools -- Write, Edit, Bash -- never on WebSearch or file reads. The arm judge is
DEBOUNCED to at most once per JUDGE_WINDOW_SECONDS (15s) per session+prompt key.

DISARM (while armed). The block clears through a SEPARATE, claim-bound decision,
not by re-running the strict arm prompt (which would keep re-firing on the original
confident sentence that lives in the transcript forever). When armed:
  1. Activity gate (deterministic, free): if the ledger shows no NEW grounding
     activity (read_paths / fetched_urls / ran_commands) since the arm, stay armed,
     no judge call.
  2. Disarm judge (only after new activity): given the flagged claim + the
     transcript, ask whether the model has NOW grounded THAT claim. If yes, disarm.
The disarm path is NOT throttled by the 15s window, so the model escapes as soon as
it has actually read the evidence.

SAFETY CAP. After BREAKER_MAX_BLOCKS consecutive blocks on one arm the breaker
fails open (disarms, logs) so a misfiring judge can never hard-lock a session.

Fails open: any judge or transcript error leaves tools unblocked.
Disable with UNIFABLE_BREAKER=0. Cap override: UNIFABLE_BREAKER_MAX_BLOCKS.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

# Mutation tools the breaker can block: writes, edits, bash (both hosts: Claude
# Code Edit/Write/MultiEdit/NotebookEdit + Bash, Codex apply_patch). WebSearch,
# Read, WebFetch, Grep and Glob are NEVER in this set, so they are never blocked.
MUTATION_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "Bash"})

# Debounce: the ARM judge fires at most once per this many seconds per key.
JUDGE_WINDOW_SECONDS = 15

# Consecutive blocks on one arm before the breaker fails open (escape hatch).
BREAKER_MAX_BLOCKS_DEFAULT = 3

# How much recent transcript the judge sees (~30k tokens of tail).
_SEGMENT_CHARS = 120_000

_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "integer",
            "enum": [0, 1],
            "description": "1 if the model stated something confidently without backing it up; else 0.",
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
    "required": ["verdict", "steering", "claim"],
    "additionalProperties": False,
}

# Release decision: has the flagged claim NOW been grounded? Separate from the arm
# judge so the strict arm prompt never re-fires on the immortal original sentence.
_DISARM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "grounded": {
            "type": "integer",
            "enum": [0, 1],
            "description": (
                "1 if the flagged claim is no longer an unbacked assertion -- it has been cited, "
                "RETRACTED, or (for a negative/absence claim) backed by a reasonable bounded search. "
                "0 only if it is still relied on AND genuinely unbacked. Never demand proof of a "
                "universal negative."
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
    "required": ["grounded", "needed"],
    "additionalProperties": False,
}

_JUDGE_SYSTEM = (
    "You are a strict groundedness monitor watching an autonomous coding agent's recent transcript "
    "(its own statements AND the tool output it has seen). Answer exactly one question: did the model "
    "say something CONFIDENTLY WITHOUT BACKING IT UP -- assert a root cause, a fix, or a fact as "
    "settled when it has not read the source, run the check, or cited evidence for it (especially "
    "after repeated failed attempts, or about an API, config, or file it never actually read)? "
    "A normal hypothesis the model is about to test is NOT a violation; only a confident, unproven "
    "assertion is. Use the tool output already in the transcript to judge grounding: if the evidence "
    "for the claim is now actually present, there is no violation. "
    "MATCH the grounding source to the claim's nature. A claim about THIS repo's code or config is "
    "grounded by reading the repo source or running the check. A claim about EXTERNAL or platform "
    "behavior -- how a host feature works (slash commands, hooks, skills), a third-party or framework "
    "API, or library semantics -- is grounded by AUTHORITATIVE EXTERNAL DOCUMENTATION the model fetches "
    "via web search / WebFetch; for such a claim the correct steering points at the official "
    "documentation to fetch, NOT at a repo file like AGENTS.md (a repo file cannot settle how an "
    "external system behaves). Never demand repo-internal evidence for a claim whose truth lives in "
    "external docs. You do NOT have a repo file listing -- describe the KIND of source that would "
    "settle the claim and let the model find the file; do not invent specific paths (name a path only "
    "if it already appears in the transcript). You can see the tool responses in the transcript: judge "
    "whether what the model actually read or fetched SUPPORTS the claim -- if it read/fetched the "
    "source but its content does not say what the model claims, that is still a violation. Do not "
    "require the model to re-quote what you can already see; reading the source is enough when the "
    "content backs the claim. "
    "Do NOT arm when: the model is retracting or correcting the claim (a withdrawn claim is not an "
    "assertion); the claim is a passing aside it is not relying on for its next action; or the claim "
    "is a negative/absence claim it has already backed with a reasonable bounded search (e.g. a grep "
    "over the relevant checkout plus reading the registry). Only arm a confident, LOAD-BEARING, "
    "unproven assertion the model is acting on. "
    "If yes: verdict=1 and write a 2-3 sentence steering prompt, addressed to the model, naming the "
    "unproven claim and telling it that its tools are restricted to read-only ones (Read, WebSearch, "
    "WebFetch, Grep, Glob) until it grounds the claim. Be blunt. "
    "If no: verdict=0 and steering MUST be the empty string. Call the function exactly once."
)

_DISARM_SYSTEM = (
    "You are a groundedness RELEASE monitor for an autonomous coding agent. The agent was earlier "
    "flagged for ONE confident, unproven claim, given to you below. Look ONLY at what the agent has "
    "since actually done in the transcript -- the source it read, the checks it ran, the file:line "
    "or command output it cited. Answer exactly one question: is the flagged claim NO LONGER an "
    "unbacked confident assertion? Set grounded=1 if ANY of these now hold: (a) it has read the "
    "source / run the check / cited file:line or command output that backs the claim; (b) it has "
    "RETRACTED or corrected the claim (a withdrawn claim is no longer asserted -- release it); "
    "(c) the claim is a NEGATIVE or absence claim ('no X', 'nothing does Y') and the model has done "
    "a REASONABLE bounded search -- e.g. a grep/rg over the relevant checkout plus reading the "
    "registry/loader -- and cited that absence; "
    "(d) the claim is about EXTERNAL or platform behavior (a host feature, third-party/framework API, "
    "or library semantics) and the model has FETCHED authoritative external documentation via web "
    "search / WebFetch whose content supports it -- official docs ground an external claim. "
    "You can see the tool responses in the transcript: judge whether what the model read or fetched "
    "ACTUALLY supports the claim. You do not need the model to re-quote it -- reading/fetching the "
    "source is enough when its content backs the claim. But if the model read/fetched the source and "
    "the content does NOT say what it claims, stay armed and say so in `needed`. "
    "You MUST NOT demand proof of a universal negative beyond a reasonable search; a bounded search "
    "that cites absence grounds a negative claim. You MUST NOT demand a repo file (e.g. AGENTS.md) "
    "for a claim whose truth lives in external documentation -- fetched official docs ground it. "
    "Judge whether the claim is still an unbacked assertion, NOT whether it is universally proven. "
    "Set grounded=0 only if the claim is still being relied on AND genuinely unbacked; then write "
    "`needed`: 1-2 sentences naming EXACTLY what is still missing -- for a repo claim, which file to "
    "read or check to run; for an external/platform claim, the official documentation to fetch. "
    "Judge only the named claim. Call the function once."
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def enabled() -> bool:
    return os.environ.get("UNIFABLE_BREAKER", "1").strip().lower() not in ("0", "false", "no", "off")


def max_blocks() -> int:
    """Consecutive blocks on one arm before the breaker fails open."""
    try:
        return max(1, int(os.environ.get("UNIFABLE_BREAKER_MAX_BLOCKS", BREAKER_MAX_BLOCKS_DEFAULT)))
    except (TypeError, ValueError):
        return BREAKER_MAX_BLOCKS_DEFAULT


def is_mutation_tool(tool_name: str) -> bool:
    """True for Write/Edit/Bash-family tools the breaker may block; False for
    WebSearch, Read, WebFetch, Grep, Glob and anything else."""
    return tool_name in MUTATION_TOOLS


# ---------------------------------------------------------------------------
# Transcript segment (what the model said + what tools returned)
# ---------------------------------------------------------------------------

def _encode_cwd(cwd: str) -> str:
    # Claude Code encodes the project dir as the path with '/' and '_' -> '-'.
    return cwd.replace("/", "-").replace("_", "-")


def locate_transcript(input_data: dict) -> str | None:
    """Prefer the hook-provided transcript_path; else derive the session jsonl
    under ~/.claude/projects/<encoded-cwd>/<session_id>.jsonl."""
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


def _flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    out.append(str(block["text"]))
                elif btype == "tool_use":
                    out.append(f"<tool_use {block.get('name')} {json.dumps(block.get('input', {}))[:300]}>")
                elif btype == "tool_result":
                    out.append(f"<tool_result {_flatten_content(block.get('content'))[:600]}>")
        return " ".join(out)
    return ""


def transcript_segment(input_data: dict, max_chars: int = _SEGMENT_CHARS) -> str:
    """The tail of the session transcript as text. Empty string on any miss."""
    path = locate_transcript(input_data)
    if not path:
        return ""
    parts: list[str] = []
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        msg = entry.get("message")
        role = entry.get("type") or (msg.get("role") if isinstance(msg, dict) else "") or "?"
        content = msg.get("content") if isinstance(msg, dict) else None
        text = _flatten_content(content)
        if text.strip():
            parts.append(f"[{role}] {text}")
    return "\n".join(parts)[-max_chars:]


# ---------------------------------------------------------------------------
# Judge (GPT-realtime-2 via codex_judge.ask_structured)
# ---------------------------------------------------------------------------

JudgeFn = Callable[[str, str, dict], dict]


def _default_judge(system: str, user: str, schema: dict) -> dict:
    from codex_judge import ask_structured

    return ask_structured(system, user, schema, schema_name="groundedness")


def judge_segment(segment: str, judge: JudgeFn | None = None) -> tuple[int, str]:
    """Ask the judge whether the model asserted something confidently without
    backing it up. Returns (1, steering) on a violation, else (0, ''). `judge` is
    injectable for tests; default is GPT-realtime-2. Empty segment -> (0, '')."""
    verdict, steering, _claim = arm_judge(segment, judge=judge)
    return verdict, steering


def arm_judge(segment: str, judge: JudgeFn | None = None) -> tuple[int, str, str]:
    """ARM decision: (verdict, steering, claim). verdict 1 means a confident,
    unproven claim; claim names it for the later release check. Empty segment ->
    (0, '', '')."""
    if not segment.strip():
        return 0, "", ""
    fn = judge or _default_judge
    obj = fn(_JUDGE_SYSTEM, segment, _JUDGE_SCHEMA)
    verdict = 1 if int(obj.get("verdict", 0) or 0) == 1 else 0
    steering = str(obj.get("steering", "") or "") if verdict == 1 else ""
    claim = str(obj.get("claim", "") or "") if verdict == 1 else ""
    return verdict, steering, claim


def disarm_judge(claim: str, segment: str, judge: JudgeFn | None = None) -> tuple[bool, str]:
    """RELEASE decision: has the flagged `claim` now been grounded by what the
    model has read/run/cited in `segment`? Returns (grounded, needed). `needed` is
    guidance on exactly what is still missing when not grounded; '' when grounded.
    Empty segment -> (False, '') (stay armed)."""
    if not segment.strip():
        return False, ""
    fn = judge or _default_judge
    user = f"FLAGGED CLAIM:\n{claim}\n\nTRANSCRIPT (what the model has since read/run/cited):\n{segment}"
    obj = fn(_DISARM_SYSTEM, user, _DISARM_SCHEMA)
    grounded = int(obj.get("grounded", 0) or 0) == 1
    needed = str(obj.get("needed", "") or "") if not grounded else ""
    return grounded, needed


# ---------------------------------------------------------------------------
# Debounced state (stored in the per-session ledger)
# ---------------------------------------------------------------------------

def breaker_key(session_id: str, active_task: str) -> str:
    """Debounce key: session + user-prompt (the pinned active_task prompt hash)."""
    return f"{session_id or 'no-session'}|{active_task or ''}"


def should_judge(state: dict, key: str, now: float, window: float = JUDGE_WINDOW_SECONDS) -> bool:
    """Debounce predicate for the ARM judge (only consulted while disarmed): judge
    if the key changed (new user prompt) or at least `window` seconds have elapsed
    since the last arm judge call for this key."""
    if state.get("breaker_key") != key:
        return True
    last = state.get("breaker_judged_at") or 0.0
    try:
        return (now - float(last)) >= window
    except (TypeError, ValueError):
        return True


def activity_total(state: dict) -> int:
    """Count of grounding actions logged in the ledger (read_paths + fetched_urls
    + ran_commands). Grows as the model reads, fetches and runs commands."""
    return (
        len(state.get("read_paths") or [])
        + len(state.get("fetched_urls") or [])
        + len(state.get("ran_commands") or [])
    )


def arm(state: dict, key: str, now: float, steering: str, claim: str) -> None:
    """Arm the breaker and snapshot the claim + activity baseline."""
    state["breaker_key"] = key
    state["breaker_judged_at"] = now
    state["breaker_armed"] = True
    state["breaker_steering"] = steering
    state["breaker_claim"] = claim
    state["breaker_armed_at"] = now
    state["breaker_activity_at_arm"] = activity_total(state)
    state["breaker_block_count"] = 0


def disarm(state: dict) -> None:
    """Clear all breaker arm state (release the block)."""
    state["breaker_armed"] = False
    state["breaker_steering"] = ""
    state["breaker_claim"] = ""
    state["breaker_armed_at"] = 0.0
    state["breaker_activity_at_arm"] = 0
    state["breaker_block_count"] = 0


def record_verdict(state: dict, key: str, now: float, verdict: int, steering: str, claim: str = "") -> None:
    """ARM-path bookkeeping: arm on verdict 1, else record the judge time and stay
    disarmed (so the debounce window advances)."""
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


# ---------------------------------------------------------------------------
# Top-level decision (called from pre_tool_use on EVERY tool)
# ---------------------------------------------------------------------------

def evaluate(
    input_data: dict,
    state: dict,
    now: float,
    active_task: str,
    judge: JudgeFn | None = None,
) -> tuple[bool, str]:
    """Run on every PreToolUse. Returns (block, steering) for the CURRENT tool.

    - While DISARMED: the strict arm judge fires at most once per
      JUDGE_WINDOW_SECONDS per session+prompt key (debounced). verdict 1 arms and
      captures the claim + activity baseline.
    - While ARMED: a separate, claim-bound release check runs (NOT time-throttled).
      It only calls the disarm judge after NEW grounding activity appears in the
      ledger; on grounded it disarms. A new user prompt (key change) drops the
      stale arm and re-evaluates.
    - block is True only when the current tool is a mutation tool (Write/Edit/Bash)
      AND the breaker is armed. WebSearch and file reads are never blocked.
    - After max_blocks() consecutive blocks on one arm, fail open (disarm + log).

    Fails open (returns (False, '')) on any judge or transcript error, and when
    UNIFABLE_BREAKER=0.
    """
    if not enabled():
        return False, ""
    tool = str(input_data.get("tool_name") or "")
    key = breaker_key(str(input_data.get("session_id") or ""), str(active_task or ""))
    try:
        armed = bool(state.get("breaker_armed"))
        if armed and state.get("breaker_key") != key:
            disarm(state)  # new user prompt: drop the stale arm, re-evaluate fresh
            armed = False
        if armed:
            # Release path: only consult the disarm judge after NEW grounding
            # activity, and never throttle it on the debounce window.
            if activity_total(state) > int(state.get("breaker_activity_at_arm") or 0):
                grounded, needed = disarm_judge(
                    str(state.get("breaker_claim") or ""),
                    transcript_segment(input_data),
                    judge=judge,
                )
                if grounded:
                    disarm(state)
                elif needed:
                    # still blocked: refresh the steering with exactly what is
                    # still missing, so the model sees concrete next steps.
                    state["breaker_steering"] = needed
        elif should_judge(state, key, now):
            verdict, steering, claim = arm_judge(transcript_segment(input_data), judge=judge)
            record_verdict(state, key, now, verdict, steering, claim)
    except Exception:
        return False, ""  # fail open on any judge/transcript failure
    if is_mutation_tool(tool) and state.get("breaker_armed"):
        count = int(state.get("breaker_block_count") or 0) + 1
        state["breaker_block_count"] = count
        if count >= max_blocks():
            _release_log(count)
            disarm(state)  # escape hatch: a misfiring judge can't hard-lock
            return False, ""
        return True, str(state.get("breaker_steering") or "")
    return False, ""
