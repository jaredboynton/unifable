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

PROVISIONAL LIFT (while armed, not yet fully grounded). The release judge may
grant a temporary lift when the model is pursuing the verification it was steered
toward. Mutations are allowed within lift_scope; the block cap is paused. While
lifted, a monitor judge on mutation PreToolUse emits advisory hints for minor
scope drift and re-arms only for egregious unrelated work.

SAFETY CAP. After BREAKER_MAX_BLOCKS consecutive blocks on one arm the breaker
fails open (disarms, logs) so a misfiring judge can never hard-lock a session.

Fails open: any judge or transcript error leaves tools unblocked.
Always on (no env disable). Cap override: UNIFABLE_BREAKER_MAX_BLOCKS.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from breaker_state import (
    adjudicated_claims,
    append_event,
    claim_already_adjudicated,
    clear_provisional_lift,
    lift_provisional,
    reinstate,
    render_events,
)
from transcript_tail import (
    JUDGE_EFFECTIVE_MAX_CHARS,
    TRANSCRIPT_TOKEN_BUDGET,
    cap_judge_message,
    fit_judge_user_message,
    stripped_transcript_tail,
    tail_tokens,
)

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

# Harness-self-referential claims (gate waiver, spec state, breaker state) cannot be
# grounded via read-only research without circularity -- never arm on them.
_HARNESS_SELF_REF_RE = re.compile(
    r"\b("
    r"unifable|fablize|evidence[\s-]spec|evidence[\s-]gate|groundedness[\s-]breaker|"
    r"pre[\s-]?(?:edit|tooluse)[\s-]?gate|gate_stop|gate_prompt|gate_post_tool|"
    r"hooks\.json|UNIFABLE_|"
    r"(?:quick\s*/?\s*)?LIGHT(?:\s+(?:mode|grade|task))?|"
    r"provisional[\s-]lift|spec[\s-]waiver|goal_seeded|"
    r"breaker[\s-]?(?:armed|open|block|lift)|unifable[\s-]spec|"
    r"unproven[\s-]claim.*(?:waiv|LIGHT|spec[\s-]task|provisional)"
    r")\b",
    re.I,
)

# Evidence-spec task board narration (T7 validated, [OK] T7, breaker OPEN, etc.).
_TASK_BOARD_STATUS_CLAIM_RE = re.compile(
    r"(?:"
    r"\bT\d+\b[^\n.]{0,100}\b(?:validated|retracted|failed|disputed|superseded|"
    r"\[OK\]|\[XX\]|\[--\]|\[~~\]|flipped\s+to|already\s+(?:done|validated|ok))"
    r"|(?:validated|retracted|failed|\[OK\]|\[XX\]|already\s+(?:done|validated))"
    r"[^\n.]{0,60}\bT\d+\b"
    r"|breaker\s*:\s*(?:OPEN|CLOSED)"
    r"|all\s+tasks\s+validated"
    r"|completion\s+breaker\s+(?:open|closed)"
    r"|task\s+board"
    r"|judge\s+(?:accepted|rejected)\s+(?:the\s+)?evidence"
    r")",
    re.I,
)

_TASK_ID_RE = re.compile(r"\bT(\d+)\b", re.I)

_SPEC_BOARD_BEGIN = "=== EVIDENCE SPEC BOARD (authoritative task status) ==="
_SPEC_BOARD_END = "=== END EVIDENCE SPEC BOARD ==="
_SPEC_BOARD_MAX = 12_000
_USER_GOAL_MAX = 400


def is_harness_self_referential(text: str) -> bool:
    """True when text is about unifable gate/hook/spec-board state."""
    t = str(text or "")
    if _HARNESS_SELF_REF_RE.search(t):
        return True
    return is_task_board_status_claim(t)


def is_task_board_status_claim(text: str) -> bool:
    """True when text asserts evidence-spec task status (T7 validated, breaker OPEN, etc.)."""
    return bool(_TASK_BOARD_STATUS_CLAIM_RE.search(str(text or "")))


# A skill loaded via the Skill tool injects its own documentation as the
# authoritative definition of what that skill does. That content is NOT in the
# breaker judge's transcript view (only "Successfully loaded skill" is), so a
# claim that merely paraphrases a just-loaded skill ("the release skill handles
# X") looks like an ungrounded confident assertion and falsely arms the breaker.
# Such a claim cannot be grounded by read-only research without re-reading the
# skill the harness just handed the model -- treat it like harness self-reference.
_SKILL_TOOL_USE_RE = re.compile(r"\[tool_use name=Skill\][^\n]*\n([^\n]+)")
_QUOTED_VALUE_RE = re.compile(r'"([^"\\]+)"')


def loaded_skill_names(segment: str) -> set[str]:
    """Skill names loaded via the Skill tool in the transcript segment."""
    names: set[str] = set()
    for m in _SKILL_TOOL_USE_RE.finditer(str(segment or "")):
        line = m.group(1).strip()
        parsed: Any = None
        try:
            parsed = json.loads(line)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            for value in parsed.values():
                if isinstance(value, str) and value.strip():
                    names.add(value.strip().lower())
        elif isinstance(parsed, str) and parsed.strip():
            names.add(parsed.strip().lower())
        else:
            for value in _QUOTED_VALUE_RE.findall(line):
                if value.strip():
                    names.add(value.strip().lower())
    return names


def claim_describes_loaded_skill(claim: str, segment: str) -> bool:
    """True when the claim attributes behavior to a skill just loaded via Skill.

    Requires explicit skill context ("<name> skill" / "skill <name>") so a repo
    claim that merely reuses a skill-name word (e.g. 'the release workflow') is
    not suppressed -- only paraphrases of the loaded skill's own behavior are.
    """
    c = str(claim or "").strip().lower()
    if not c:
        return False
    names = loaded_skill_names(segment)
    if not names:
        return False
    for name in names:
        n = re.escape(name)
        if re.search(rf"\b{n}\b[\s\-]*skills?\b", c):
            return True
        if re.search(rf"\bskills?[\s:(\-]*{n}\b", c):
            return True
    return False


def _task_ids_in_text(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _TASK_ID_RE.finditer(str(text or "")):
        tid = f"T{m.group(1)}"
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def _extract_spec_board(segment: str) -> str:
    begin = str(segment or "").find(_SPEC_BOARD_BEGIN)
    if begin < 0:
        return ""
    start = begin + len(_SPEC_BOARD_BEGIN)
    end = segment.find(_SPEC_BOARD_END, start)
    body = segment[start:end if end >= 0 else None].strip()
    return body


def _claim_supported_by_spec_board(claim: str, segment: str) -> bool:
    """True when an evidence-spec status claim matches the injected board snapshot."""
    if not is_task_board_status_claim(claim):
        return False
    board = _extract_spec_board(segment)
    if not board:
        return False
    claim_l = claim.lower()
    for tid in _task_ids_in_text(claim):
        tid_pat = re.escape(tid)
        if re.search(rf"\[OK\]\s*{tid_pat}\b", board, re.I):
            if re.search(r"\b(valid|ok|done|accept|pass|flip)", claim_l):
                return True
        if re.search(rf"\[XX\]\s*{tid_pat}\b", board, re.I):
            if re.search(r"\b(fail|reject|xx|not\s+valid)", claim_l):
                return True
        if re.search(rf"\[~~\]\s*{tid_pat}\b", board, re.I):
            if re.search(r"\b(retract|impossib)", claim_l):
                return True
        if re.search(rf"\[--\]\s*{tid_pat}\b", board, re.I):
            if re.search(r"\b(pending|open|not\s+yet)", claim_l):
                return True
    if re.search(r"breaker\s*:\s*OPEN", board, re.I) and re.search(
        r"breaker\s*(?:open|all\s+tasks\s+validated)", claim_l
    ):
        return True
    if re.search(r"breaker\s*:\s*CLOSED", board, re.I) and re.search(
        r"breaker\s*closed", claim_l
    ):
        return True
    return False


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
                "claims the model retracted or labeled uncertain. Claims about unifable/fablize "
                "HARNESS state (LIGHT/quick waiver, evidence spec validation, provisional lift, "
                "hook block messages, breaker armed/disarmed, whether edits are allowed) are "
                "self-referential and NOT verifiable -- set load_bearing=0. When load_bearing=0, "
                "verdict MUST be 0."
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
                "WebFetch, Grep, Glob) and whitelisted research Bash (cd, ls, glob, rg, the explore "
                "skill's trace.sh, unifusion skill scripts, spec CLI) until it grounds the claim, and describe the KIND of "
                "evidence that would "
                "disarm it -- you do NOT have a repo listing, so do not invent file paths. NEVER "
                "steer the model to run a command that the breaker blocks (node, npm test, edits); "
                "prefer reading source files, result fields, and fixture thresholds already in the "
                "repo. For a claim about THIS repo's code/config, say what files to read. For "
                "EXTERNAL or platform/API behavior, steer in order: (1) authoritative "
                "documentation (web search / WebFetch) when it exists; (2) community prior art "
                "where others have reverse-engineered the same behavior (GitHub repos, gists, "
                "issues, blog posts -- WebSearch/WebFetch); (3) if nothing recent or trustworthy "
                "is found, tell the model to dig in and start empirical reverse-engineering "
                "(capture/read an actual response: HTTP body fields, status, sample payload). "
                "Prior art is a starting point, not a substitute for verifying behavior that "
                "matters to the user goal. NEVER steer toward verifying unifable/fablize harness "
                "gate state (LIGHT waiver, spec tasks, provisional lift, hook messages) -- those "
                "claims are self-referential and must not arm. Do NOT insist on official docs alone "
                "when community RE or fresh probing is the correct path. NEVER steer toward blocked "
                "shell "
                "commands. Name a specific path only if it already appears in the transcript. "
                "Empty when verdict=0."
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
                "(no longer blocks the current work). For external algorithm/format claims, also set "
                "grounded=1 when transcript tool output demonstrably validates the claim (e.g. "
                "decrypt yields a correctly formatted token, API returns expected schema/fields, "
                "reverse-engineered endpoint response includes the claimed payload, community "
                "prior art (GitHub/gist/issue/blog) documents the claimed behavior and is cited "
                "in the transcript, check exit 0 with expected fields) even if authoritative "
                "docs are absent or still being fetched. Prior art alone may release when it "
                "directly supports the specific claim; otherwise it satisfies a 'starting point' "
                "steering step and empirical proof may still be needed for load-bearing "
                "functional claims. Official documentation is optional when empirical or prior-art "
                "proof is already present. 0 only if "
                "load_bearing=1 AND the claim is still relied on AND genuinely unbacked. Never demand "
                "proof of a universal negative."
            ),
        },
        "needed": {
            "type": "string",
            "description": (
                "When grounded=0 and provisional_release=0, a 1-2 sentence instruction addressed to "
                "the model naming EXACTLY what is still missing to disarm, matched to the claim: "
                "for a repo claim, which file(s) to read (never a blocked shell command -- the "
                "breaker forbids mutating Bash); for an external/platform/API claim, missing "
                "official documentation, community prior-art RE (GitHub/issues/blogs), and/or "
                "empirical proof (actual response showing claimed fields); if searches found "
                "nothing recent, steer to start digging with read-only probes -- not blocked "
                "shell. Empty when grounded=1 or provisional_release=1."
            ),
        },
        "provisional_release": {
            "type": "integer",
            "enum": [0, 1],
            "description": (
                "1 ONLY when grounded=0, load_bearing=1, and the transcript shows the model is "
                "actively pursuing the verification described in prior NEEDED/steering (reads/fetches "
                "cited, retractions honored, minimal config edit to run a user-requested experiment) "
                "but is not yet fully grounded. Do NOT set for outcome predictions ('scores will "
                "hold') -- those need full disarm via evidence or load_bearing=0. When "
                "provisional_release=1, lift_reason and lift_scope MUST be non-empty."
            ),
        },
        "lift_reason": {
            "type": "string",
            "description": (
                "When provisional_release=1, 1-2 sentences for the model explaining why the breaker "
                "opened temporarily. Empty otherwise."
            ),
        },
        "lift_scope": {
            "type": "string",
            "description": (
                "When provisional_release=1, what work is allowed while lifted. Must cover the minimal "
                "implementation steps toward USER GOAL that apply the knowledge being verified (scripts, "
                "one-off checks, temp files) -- not 'read-only only' when the user goal requires "
                "execution. Empty otherwise."
            ),
        },
    },
    "required": [
        "load_bearing", "grounded", "needed", "provisional_release", "lift_reason", "lift_scope",
    ],
    "additionalProperties": False,
}

_MONITOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "drift_level": {
            "type": "integer",
            "enum": [0, 1, 2],
            "description": (
                "0 if on track: imminent tool and transcript show work within lift_scope, advancing "
                "USER GOAL, or pursuing verification of the flagged claim. 1 for minor drift worth a "
                "nudge (slightly outside lift_scope but still goal-adjacent) -- hint only, never block. "
                "2 ONLY for egregious off-track work: clearly unrelated refactors, new confident "
                "ungrounded claims, or abandoning verification entirely. When uncertain, prefer 0 or 1 "
                "over 2."
            ),
        },
        "feedback": {
            "type": "string",
            "description": (
                "When drift_level=1, ONE concrete advisory nudge (never blocks). "
                "When drift_level=2, 1-2 sentences re-arming guidance. "
                "Empty when drift_level=0."
            ),
        },
    },
    "required": ["drift_level", "feedback"],
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
    "HARNESS SELF-REFERENCE (never arm): Claims about unifable/fablize ITSELF -- whether the run "
    "is waived under LIGHT/quick mode, whether the evidence spec is satisfied, whether a "
    "provisional lift exists, what a PreToolUse/Stop hook message means, whether edits are "
    "currently allowed, OR narration of evidence-spec TASK BOARD status (T7 validated, "
    "[OK] T7, breaker OPEN/CLOSED, task validated this cycle) -- are self-referential. "
    "The segment includes an EVIDENCE SPEC BOARD block when a spec exists; use it to "
    "verify task status instead of arming. Set load_bearing=0 and verdict=0; "
    "steering MUST be empty. Only arm on claims about the USER's repo, external systems, or "
    "domain facts the user asked about. "
    "LOADED SKILL (never arm): a claim that paraphrases what a SKILL just loaded via the Skill "
    "tool does (e.g. 'the release skill handles the full release tail') describes "
    "harness-provided instructions the model was just handed, not the user's repo or an "
    "external system. Its content is authoritative and cannot be re-grounded by read-only "
    "research; set load_bearing=0 and verdict=0. "
    "(B) UNGROUNDED CONFIDENT ASSERTION? Only if load_bearing=1, ask whether the model asserted a "
    "root cause, fix, or fact as SETTLED without reading the source, running the check, or citing "
    "evidence (especially about an API, config, or file it never actually read). A normal hypothesis "
    "the model is about to test is NOT a violation. Use tool output already in the transcript: if "
    "evidence for the claim is present, verdict=0. "
    "MATCH the grounding source to the claim's nature. Repo code/config claims are "
    "grounded by reading source or running checks. EXTERNAL or platform claims -- "
    "third-party APIs, undocumented endpoints, host behavior -- may be grounded by "
    "(1) authoritative documentation via web search / WebFetch, (2) community prior art where "
    "others have reverse-engineered the same behavior (GitHub repos, gists, issues, writeups) "
    "when cited in the transcript, OR (3) empirical reverse-engineering: tool output showing "
    "the actual response (field names, schema, status) or a hypothesis the model is about to "
    "test with a read-only probe. When arming, steer external claims toward docs, then prior "
    "art, then -- if nothing recent or trustworthy exists -- tell the model to dig in and start "
    "empirical RE rather than stalling on missing official docs. Do NOT arm when confirmation "
    "is already in the transcript (docs, prior art, or probe output), when the model marks the "
    "claim as hypothesis-to-test, or when it appropriately labels reverse-engineered behavior "
    "as verified-by-probe rather than settled-by-docs. Do NOT require official docs for "
    "behavior that can only be verified empirically or via community RE. NEVER demand repo "
    "files for external truth when docs do not exist and probing is appropriate. You do NOT "
    "have a repo file listing -- describe the KIND of evidence that would settle the claim; "
    "name a path only if it already appears in the transcript. Judge whether what the model "
    "read, fetched, cited, or empirically observed SUPPORTS the claim. "
    "ARM ONLY when load_bearing=1 AND the assertion is genuinely ungrounded: verdict=1, name the "
    "claim, write a 2-3 sentence steering prompt telling the model its tools are restricted to "
    "read-only ones (Read, WebSearch, WebFetch, Grep, Glob) and whitelisted research Bash "
    "(cd, ls, glob, rg, the explore skill's trace.sh, unifusion skill scripts, spec CLI) until it grounds THAT claim. NEVER "
    "steer toward running "
    "a blocked command (node, npm test, mutating shell) to prove a repo claim -- point at files "
    "to read instead. Otherwise verdict=0, load_bearing=0 or 1 as appropriate, steering MUST be "
    "empty string, claim MUST be empty. Call the function exactly once."
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
    "the immediate repo edit/check, claims about unifable/fablize harness gate state (LIGHT "
    "waiver, spec validation, provisional lift, hook block semantics), or otherwise not needed "
    "for the work NOW in the transcript. "
    "Set load_bearing=1 only if the model still relies on that claim for the immediate next action. "
    "(B) SHOULD THE BREAKER RELEASE? Set grounded=1 if ANY hold: (1) load_bearing=0 -- release "
    "without requiring further evidence; (2) the claim was RETRACTED or corrected, or the model "
    "superseded part of a compound claim and no longer relies on the retracted portion; (3) the "
    "model read the source / cited file:line or tool output that backs the claim -- including "
    "deriving numeric scores by applying formulas visible in Read source to fields visible in "
    "Read result files (do NOT require re-running a blocked scorer command); (4) negative/absence "
    "claim backed by a reasonable bounded search; (5) external/platform/API claim backed by "
    "fetched authoritative documentation, cited community prior-art RE (GitHub/gist/issue/blog "
    "documenting the claimed behavior), OR empirical reverse-engineering output in the transcript "
    "(actual API response with claimed fields, successful probe with cited output); "
    "(6) empirical validation -- for external algorithm/format/endpoint claims, transcript "
    "tool output demonstrably validates the claim (decrypt yields correctly formatted token, "
    "API returns expected schema/fields). Official docs are not required when prior-art or "
    "empirical proof is present. When load_bearing=0, grounded MUST be 1. Set grounded=0 ONLY "
    "when load_bearing=1 AND the claim is still relied on AND genuinely unbacked; then write "
    "`needed` naming files to read, never a blocked shell command. When grounded=1, needed MUST "
    "be empty. "
    "(C) PROVISIONAL RELEASE? When grounded=0 AND load_bearing=1, check whether the model is "
    "pursuing the verification the breaker requested (reading cited artifacts, fetching docs, "
    "searching GitHub/community prior-art RE, running empirical probes, capturing API responses, "
    "retracting outcome claims, making the minimal config edit needed to run a user-requested "
    "check) rather than asserting future outcomes as settled. If so, set provisional_release=1 "
    "with lift_reason (why you opened temporarily) and lift_scope (allowed work toward USER GOAL). "
    "lift_scope must cover minimal scripts/checks needed to apply verified knowledge toward the "
    "user goal, not read-only-only when execution is required. Do NOT repeat "
    "the same needed if the model already did those reads -- lift instead; if an empirical run "
    "already succeeded, prefer full disarm (grounded=1) over another narrow lift. Do NOT lift when the "
    "only missing proof requires a blocked run whose purpose IS measuring the outcome; lift only "
    "for experiment setup the user requested. Judge only the named claim. Call the function once."
)

_MONITOR_SYSTEM = (
    "You are a provisional-lift MONITOR for an autonomous coding agent. The breaker was temporarily "
    "opened so the agent could pursue verification within a bounded scope. The USER GOAL, FLAGGED "
    "CLAIM, LIFT SCOPE, IMMINENT TOOL, and transcript are below. "
    "Set drift_level=0 when on track: the imminent tool advances USER GOAL, stays within lift_scope, "
    "or pursues verification of the flagged claim. Work that applies already-verified or empirically "
    "validated knowledge toward the user goal is ON TRACK -- e.g. using extracted cookies to call an "
    "API is not the same as asserting a decrypt algorithm without source. Do NOT penalize completed "
    "empirical steps that already produced valid tool output when the imminent tool is downstream "
    "verification or goal progress. "
    "Set drift_level=1 for minor drift worth an advisory nudge (slightly outside lift_scope but still "
    "goal-adjacent). Write ONE concrete message in feedback; it is ADVISORY ONLY and never blocks. "
    "Set drift_level=2 ONLY for egregious off-track work: clearly unrelated refactors, new confident "
    "ungrounded claims, or abandoning verification entirely. Write re-arming guidance in feedback. "
    "When uncertain, prefer drift_level=0 or 1 over 2. When drift_level=0, feedback MUST be empty. "
    "Call the function once."
)

_SCOPE_HINT_PREFIX = "Hint: "


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
    """Merged judge input: breaker events + transcript tail + spec board + fresh tool.

    The spec board and fresh tool output are reserved at the end so tail truncation
    does not drop authoritative task status.
    """
    from transcript_tail import MAX_CHARS_PER_TOKEN

    head_parts: list[str] = []
    rendered = render_events(events)
    if rendered:
        head_parts.append(rendered.rstrip())

    board = _spec_board_block(input_data)
    tail_parts: list[str] = []
    if board:
        tail_parts.append(board.rstrip())
    if fresh_tool and fresh_tool.strip():
        tail_parts.append(
            '<record line="000000" type="fresh_tool" role="tool">\n'
            + fresh_tool.strip()
            + "\n</record>"
        )

    reserve_chars = sum(len(p) + 2 for p in tail_parts)
    host_budget_chars = max(
        2000,
        (max_tokens * MAX_CHARS_PER_TOKEN) - reserve_chars - sum(len(p) + 2 for p in head_parts),
    )
    host = transcript_segment(input_data, max_tokens=max_tokens)
    if host:
        if len(host) > host_budget_chars:
            host = host[-host_budget_chars:]
        head_parts.append(host.rstrip())

    if not head_parts and not tail_parts:
        return ""
    combined = "\n\n".join(head_parts + tail_parts)
    return tail_tokens(combined, max_tokens)


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
        append = (
            f"\n\nDo NOT flag any of the following claims as they have already been "
            f"adjudicated or grounded:\n{claims_str}"
        )
        room = JUDGE_EFFECTIVE_MAX_CHARS - len(system)
        if room > 0:
            system += cap_judge_message(append, room)
    obj = fn(system, segment, _JUDGE_SCHEMA)
    load_bearing = int(obj.get("load_bearing", 0) or 0) == 1
    verdict = 1 if int(obj.get("verdict", 0) or 0) == 1 else 0
    if verdict == 1 and not load_bearing:
        verdict = 0
    steering = str(obj.get("steering", "") or "") if verdict == 1 else ""
    claim = str(obj.get("claim", "") or "") if verdict == 1 else ""
    if verdict == 1 and _claim_supported_by_spec_board(claim, segment):
        return 0, "", ""
    if verdict == 1 and (
        is_harness_self_referential(claim)
        or is_harness_self_referential(steering)
        or is_task_board_status_claim(claim)
    ):
        return 0, "", ""
    if verdict == 1 and claim_describes_loaded_skill(claim, segment):
        return 0, "", ""
    return verdict, steering, claim


def _spec_board_block(input_data: dict) -> str:
    """Current evidence-spec task board for breaker judges (authoritative status)."""
    try:
        from model_notify import format_spec_status
        from spec import canonical_project_root, load_spec, resolve_session_id

        cwd = canonical_project_root(input_data.get("cwd") or os.getcwd())
        session_key = resolve_session_id(input_data, default=None)
        if not session_key:
            return ""
        spec = load_spec(cwd, session_key)
        if not spec:
            return ""
        board = format_spec_status(spec, collapse_resolved=True)
        if not board.strip():
            return ""
        body = f"{_SPEC_BOARD_BEGIN}\n{board}\n{_SPEC_BOARD_END}"
        if len(body) > _SPEC_BOARD_MAX:
            body = body[: _SPEC_BOARD_MAX - 24] + "\n(spec board truncated)\n" + _SPEC_BOARD_END
        return body
    except Exception:
        return ""


def _user_goal_block(input_data: dict, active_task: str) -> str:
    """Best-effort restated goal from the session spec for judge context."""
    try:
        from spec import canonical_project_root, load_spec, resolve_session_id

        cwd = canonical_project_root(input_data.get("cwd") or os.getcwd())
        session_key = resolve_session_id(input_data, default=None)
        if not session_key:
            return ""
        spec = load_spec(cwd, session_key)
        if not spec:
            return ""
        goal = str(spec.get("restated_goal") or "").strip()
        if not goal:
            return ""
        if len(goal) > _USER_GOAL_MAX:
            return goal[: _USER_GOAL_MAX - 3] + "..."
        return goal
    except Exception:
        return ""


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
    prefix = (
        f"{goal_block}FLAGGED CLAIM:\n{claim}\n\n"
        f"TRANSCRIPT (what the model has since read/run/cited):\n"
    )
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
    prefix = (
        f"{goal_block}FLAGGED CLAIM:\n{claim}\n\nLIFT SCOPE:\n{scope}\n\n"
        f"IMMINENT TOOL:\n{tool_name}\n\nTRANSCRIPT:\n"
    )
    user = fit_judge_user_message(prefix, segment)
    obj = fn(_MONITOR_SYSTEM, user, _MONITOR_SCHEMA)
    drift = int(obj.get("drift_level", 0) or 0)
    if drift not in (0, 1, 2):
        drift = 0
    feedback = str(obj.get("feedback", "") or "").strip() if drift in (1, 2) else ""
    return drift, feedback


def _provisional_lift_message(reason: str, scope: str) -> str:
    return (
        f"unifable breaker: provisional lift — {reason} "
        f"Stay within scope: {scope}. Mutations allowed until grounded; minor drift yields "
        "advisory hints only."
    )


def _disarm_message() -> str:
    return (
        "unifable breaker open: the flagged claim is grounded. "
        "Write/Edit/Bash are unrestricted again."
    )


def _needed_message(needed: str) -> str:
    return f"unifable breaker: still armed. {needed}"


def _fail_open_message(count: int, claim: str) -> str:
    detail = f" Claim: {claim}" if claim else ""
    return (
        f"unifable breaker auto-released after {count} consecutive blocks (fail-open). "
        "The flagged claim was never grounded; Write/Edit/Bash are unrestricted again -- "
        f"verify it yourself before relying on it.{detail}"
    )


def _stale_arm_message(claim: str) -> str:
    detail = f" (claim: {claim})" if claim else ""
    return (
        "unifable breaker: cleared a stale groundedness arm from a previous "
        f"prompt/session{detail}; Write/Edit/Bash are unrestricted."
    )


def _apply_release(state: dict, claim: str, verdict: ReleaseVerdict) -> tuple[bool, str]:
    """Record release outcome on `state`. Returns (fully_disarmed, lift_notify_message)."""
    if verdict.grounded:
        append_event(state, "DISARM", claim=claim, grounded=True)
        disarm(state)
        return True, ""
    if verdict.provisional and verdict.lift_reason and verdict.lift_scope:
        notify = _provisional_lift_message(verdict.lift_reason, verdict.lift_scope)
        append_event(
            state,
            "LIFT",
            claim=claim,
            reason=verdict.lift_reason,
            scope=verdict.lift_scope,
        )
        lift_provisional(state, claim, verdict.lift_reason, verdict.lift_scope, notify)
        return False, notify
    if verdict.needed:
        append_event(state, "NEEDED", claim=claim, needed=verdict.needed)
        state["breaker_steering"] = verdict.needed
    return False, ""


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
    clear_provisional_lift(state)


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
) -> tuple[bool, str, str]:
    """PreToolUse path: arm judge (debounced) and block mutation tools while armed."""
    tool = str(input_data.get("tool_name") or "")
    key = breaker_key(str(input_data.get("session_id") or ""), str(active_task or ""))
    events = state.get("events") if isinstance(state.get("events"), list) else []
    notify_out = ""
    stale_notify = ""
    try:
        armed = bool(state.get("breaker_armed"))
        provisional = bool(state.get("breaker_provisional"))
        if (armed or provisional) and state.get("breaker_key") != key:
            stale_claim = str(state.get("breaker_claim") or "")
            append_event(state, "STALE_ARM_DROPPED", claim=stale_claim)
            disarm(state)
            armed = False
            provisional = False
            stale_notify = _stale_arm_message(stale_claim)
        if provisional:
            claim = str(state.get("breaker_claim") or "")
            user_goal = _user_goal_block(input_data, active_task)
            segment = judge_transcript(input_data, events)
            if claim and segment.strip():
                release_verdict = disarm_judge(claim, segment, user_goal=user_goal, judge=judge)
                disarmed, lift_msg = _apply_release(state, claim, release_verdict)
                if disarmed:
                    provisional = False
                    state["breaker_pending_notify"] = _disarm_message()
                elif lift_msg:
                    state["breaker_pending_notify"] = lift_msg
            if state.get("breaker_provisional") and is_mutation_tool(tool):
                scope = str(state.get("breaker_lift_scope") or "")
                if claim and scope:
                    drift, feedback = monitor_provisional_judge(
                        claim, scope, segment, tool, user_goal=user_goal, judge=judge,
                    )
                    if drift == 2:
                        append_event(state, "REINSTATE", claim=claim, corrective=feedback)
                        reinstate(state, claim, feedback or "Return to the verification scope.")
                        return True, feedback or "Return to the verification scope.", ""
                    if drift == 1 and feedback:
                        append_event(state, "SCOPE_HINT", claim=claim, hint=feedback)
                        hint_msg = f"{_SCOPE_HINT_PREFIX}{feedback}"
                        existing = str(state.get("breaker_pending_notify") or "")
                        state["breaker_pending_notify"] = (
                            f"{existing}\n{hint_msg}".strip() if existing else hint_msg
                        )
            pending = str(state.get("breaker_pending_notify") or "")
            if pending:
                state["breaker_pending_notify"] = ""
                notify_out = pending
            return False, "", notify_out
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
                user_goal = _user_goal_block(input_data, active_task)
                release_verdict = disarm_judge(claim, segment, user_goal=user_goal, judge=judge)
                disarmed, lift_msg = _apply_release(state, claim, release_verdict)
                if disarmed:
                    state["breaker_pending_notify"] = _disarm_message()
                elif lift_msg:
                    state["breaker_pending_notify"] = lift_msg
                elif release_verdict.needed:
                    state["breaker_pending_notify"] = _needed_message(release_verdict.needed)
    except Exception:
        return False, "", ""
    if is_mutation_tool(tool) and state.get("breaker_armed"):
        count = int(state.get("breaker_block_count") or 0) + 1
        state["breaker_block_count"] = count
        if count >= max_blocks():
            _release_log(count)
            claim = str(state.get("breaker_claim") or "")
            append_event(state, "FAIL_OPEN", claim=claim, block_count=count)
            disarm(state)
            return False, "", _fail_open_message(count, claim)
        state["breaker_pending_notify"] = ""
        return True, str(state.get("breaker_steering") or ""), ""
    pending = str(state.get("breaker_pending_notify") or "")
    if pending:
        state["breaker_pending_notify"] = ""
        notify_out = pending
    if stale_notify:
        notify_out = f"{stale_notify}\n{notify_out}".strip() if notify_out else stale_notify
    return False, "", notify_out


def evaluate_post_tool_release(
    input_data: dict,
    state: dict,
    fresh_tool: str,
    active_task: str = "",
    judge: JudgeFn | None = None,
) -> tuple[bool, str, str]:
    """PostToolUse release path. Returns (fully_disarmed, needed, context_message)."""
    armed = bool(state.get("breaker_armed"))
    provisional = bool(state.get("breaker_provisional"))
    if not armed and not provisional:
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
        user_goal = _user_goal_block(input_data, active_task)
        release_verdict = disarm_judge(claim, segment, user_goal=user_goal, judge=judge)
        disarmed, lift_msg = _apply_release(state, claim, release_verdict)
        if disarmed:
            return True, "", _disarm_message()
        if lift_msg:
            return False, "", lift_msg
        if release_verdict.needed:
            return False, release_verdict.needed, _needed_message(release_verdict.needed)
    except Exception:
        return False, "", ""
    return False, "", ""


# Backward-compatible alias for tests migrating from evaluate().
evaluate = evaluate_pre_tool
