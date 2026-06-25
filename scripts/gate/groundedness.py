#!/usr/bin/env python3
"""Overconfidence / groundedness breaker (unifable).

Two directional decisions, both by GPT-realtime-2 over merged transcript material
(host JSONL tail + prior breaker-event records + optional fresh PostToolUse output):

ARM (while disarmed). On PreToolUse the strict judge asks two questions from the
transcript: (1) did the model say something CONFIDENTLY WITHOUT BACKING IT UP, and
(2) is that assertion LOAD-BEARING for the work currently in progress (the user
request, the imminent edit/check, the decision driving the next tool)? Only when
both hold (verdict 1) does the breaker arm and block mutation tools. The arm judge
is DEBOUNCED to at most once per JUDGE_WINDOW_SECONDS (3s) per session+prompt key.
On the same call it also returns a minimal next-step DIRECTIVE and a TOOL_SCOPE
(the stepwise director): these are persisted to breaker state and enforced
deterministically by tool_scope.in_scope, with no extra judge round-trip.
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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from breaker_state import (
    adjudicated_claims,
    append_event,
    breaker_lock,
    claim_already_adjudicated,
    clear_provisional_lift,
    lift_provisional,
    load_breaker,
    reinstate,
    render_events,
    save_breaker,
)
from transcript_tail import (
    JUDGE_EFFECTIVE_MAX_CHARS,
    TRANSCRIPT_TOKEN_BUDGET,
    cap_judge_message,
    fit_judge_user_message,
    stripped_transcript_tail,
    tail_tokens,
)

try:
    from research_bash_guidance import groundedness_bash_whitelist_fragment
except ImportError:  # pragma: no cover
    from scripts.gate.research_bash_guidance import groundedness_bash_whitelist_fragment

# Mutation tools the breaker can block: writes, edits, bash (both hosts: Claude
# Code Edit/Write/MultiEdit/NotebookEdit + Bash, Codex apply_patch). WebSearch,
# Read, WebFetch, Grep and Glob are NEVER in this set, so they are never blocked.
MUTATION_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "Bash"})

# PostToolUse tools that can trigger the release judge while armed.
RELEASE_TOOLS = frozenset({"Read", "WebFetch", "WebSearch", "Grep", "Glob", "NotebookRead"})

# Debounce: the per-tool judge fires at most once per this many seconds per key.
# Stepwise harness: tightened from 15s to 3s so the director directive + tool
# scope refresh roughly every action without a round-trip on every single call.
JUDGE_WINDOW_SECONDS = 3

# Token budget for the director's minimal next-step directive (chars). Kept tight
# so per-step guidance stays cheap; the judge is told to be terse, this enforces it.
DIRECTIVE_MAX_CHARS = 400

# Coalesce window: once any judge has fired for a key, concurrent PreToolUse
# processes from the same parallel tool-call batch (which all judge the identical
# transcript and so get the identical verdict) skip their own judge call and
# reuse the persisted breaker state. Short, so it catches the simultaneous burst
# without changing steady-state release cadence. Override: UNIFABLE_JUDGE_COALESCE_WINDOW.
try:
    JUDGE_COALESCE_WINDOW_SECONDS = float(os.environ.get("UNIFABLE_JUDGE_COALESCE_WINDOW", "2.0") or "2.0")
except (TypeError, ValueError):
    JUDGE_COALESCE_WINDOW_SECONDS = 2.0

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


_REPO_PATH_IN_TEXT_RE = re.compile(
    r"\b(?:[\w.-]+/)+[\w.-]+\.(?:py|md|json|toml|sh|yaml|yml)(?::\d+)?\b",
    re.I,
)
_HYPOTHESIS_PHRASE_RE = re.compile(
    r"\b("
    r"lives?\s+in|is\s+(?:implemented\s+)?in|likely\s+in|probably\s+in|"
    r"appears?\s+to\s+be\s+in|seems?\s+to\s+be\s+in|should\s+be\s+in|"
    r"I(?:'ll|\s+will)\s+(?:read|check|look)|let\s+me\s+(?:read|check|look|explore)|"
    r"I(?:'m|\s+am)\s+going\s+to\s+(?:read|check|look)"
    r")\b",
    re.I,
)
_READ_TOOL_USE_RE = re.compile(
    r"\[tool_use name=(?:Read|Grep|Glob|NotebookRead)[^\]]*\][\s\S]{0,800}",
    re.I,
)


def _norm_repo_path(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./").split(":", 1)[0]


def paths_in_text(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _REPO_PATH_IN_TEXT_RE.finditer(str(text or "")):
        norm = _norm_repo_path(match.group(0))
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _path_targets_match(left: str, right: str) -> bool:
    a = _norm_repo_path(left)
    b = _norm_repo_path(right)
    if not a or not b:
        return False
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _imminent_read_target(input_data: dict | None) -> str:
    if not isinstance(input_data, dict):
        return ""
    tool = str(input_data.get("tool_name") or "")
    if tool not in RELEASE_TOOLS:
        return ""
    inp = input_data.get("tool_input")
    if not isinstance(inp, dict):
        return ""
    for key in ("file_path", "path", "pattern", "glob_pattern"):
        value = str(inp.get(key) or "").strip()
        if value:
            return value
    return ""


def _segment_plans_read(segment: str, path: str) -> bool:
    norm = re.escape(_norm_repo_path(path))
    if not norm:
        return False
    for block in _READ_TOOL_USE_RE.finditer(str(segment or "")):
        if re.search(norm, block.group(0), re.I):
            return True
    if re.search(rf'file_path["\']?\s*:\s*["\'][^"\']*{norm}', segment, re.I):
        return True
    return False


def should_suppress_path_hypothesis_arm(
    claim: str,
    segment: str,
    input_data: dict | None = None,
) -> bool:
    """Skip arming when a planning hypothesis names a path the agent is about to read."""
    if not _HYPOTHESIS_PHRASE_RE.search(str(claim or "")):
        return False
    paths = paths_in_text(claim)
    if not paths:
        return False
    imminent = _imminent_read_target(input_data)
    if imminent and any(_path_targets_match(p, imminent) for p in paths):
        return True
    tail = str(segment or "")[-6000:]
    return any(_segment_plans_read(tail, p) for p in paths)


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
    body = segment[start : end if end >= 0 else None].strip()
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
    if re.search(r"breaker\s*:\s*OPEN", board, re.I) and re.search(r"breaker\s*(?:open|all\s+tasks\s+validated)", claim_l):
        return True
    if re.search(r"breaker\s*:\s*CLOSED", board, re.I) and re.search(r"breaker\s*closed", claim_l):
        return True
    return False


def _research_bash_whitelist_summary() -> str:
    try:
        from research_bash_guidance import bash_allowed_summary
    except ImportError:
        from scripts.gate.research_bash_guidance import bash_allowed_summary  # pragma: no cover
    return bash_allowed_summary()


def _steering_description() -> str:
    explore = groundedness_bash_whitelist_fragment()
    bash_summary = _research_bash_whitelist_summary()
    return (
        "When verdict=1, a 2-3 sentence steering prompt addressed to the model. Name the "
        "unproven claim, say its tools are restricted to read-only ones (Read, WebSearch, "
        "WebFetch, Grep, Glob) and whitelisted research Bash ("
        f"{bash_summary}, "
        f"{explore}unifusion skill scripts, spec CLI) until it grounds the claim, and describe the KIND of "
        "evidence that would "
        "disarm it -- you do NOT have a repo listing, so do not invent file paths. NEVER "
        "steer the model to run a command that the breaker blocks (node, npm test, edits); "
        "prefer reading source files, result fields, and fixture thresholds already in the "
        "repo. For a claim about THIS repo's code/config, say what files to read. For "
        "in-repo conventions already documented (version bump via just version, AGENTS.md "
        "release rules), steer to those repo files -- not SemVer.org or external docs. For "
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
    )


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
            "description": ("1 ONLY if load_bearing=1 AND the model stated something confidently without backing it up; else 0."),
        },
        "steering": {
            "type": "string",
            "description": _steering_description(),
        },
        "claim": {
            "type": "string",
            "description": (
                "When verdict=1, the ONE specific unproven claim, in 1-2 sentences, so a later "
                "release check can decide whether THAT claim has since been grounded. Empty string "
                "when verdict=0."
            ),
        },
        "verify": {
            "type": "object",
            "description": (
                "Falsifiable check that lets the breaker confirm the claim from the repo itself "
                "instead of forcing the model to re-prove a TRUE claim by hand. Populate ONLY when "
                "the claim reduces to literal substring presence/absence in named files that "
                "ALREADY appear verbatim in the transcript -- e.g. a version bump, a string added "
                "or removed, a config value. NEVER invent file paths. Leave BOTH arrays empty when "
                "the claim is not mechanically checkable this way (judgement, behavior, "
                "external/API facts, anything needing computation or reasoning). The breaker runs "
                "this read-only: if the files confirm it the claim does NOT arm; if they refute it "
                "or it is empty the verdict stands. A wrong predicate can only fail safe."
            ),
            "properties": {
                "must_contain": {
                    "type": "array",
                    "description": "Each {file, text}: the claim holds only if literal substring `text` is PRESENT in `file`.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "description": "Repo-relative path that appears in the transcript."},
                            "text": {"type": "string", "description": "Literal substring that must be present."},
                        },
                        "required": ["file", "text"],
                        "additionalProperties": False,
                    },
                },
                "must_not_contain": {
                    "type": "array",
                    "description": "Each {file, text}: the claim holds only if literal substring `text` is ABSENT from `file` (e.g. an old version fully removed).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "description": "Repo-relative path that appears in the transcript."},
                            "text": {"type": "string", "description": "Literal substring that must be absent."},
                        },
                        "required": ["file", "text"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["must_contain", "must_not_contain"],
            "additionalProperties": False,
        },
        "directive": {
            "type": "string",
            "description": (
                "STEPWISE DIRECTOR (independent of verdict): ONE short imperative sentence telling "
                "the model exactly what to do NEXT toward the goal -- the single most useful next "
                "action given the transcript and the spec board (e.g. 'Read scripts/gate/spec.py to "
                "confirm the task schema before editing.', 'Run the failing check and paste its "
                "output.', 'Restate the goal, then add the first requirement.'). Be terse and "
                "concrete; name a file/command only if it already appears in the transcript or board. "
                "Empty only when there is genuinely nothing to add."
            ),
        },
        "tool_scope": {
            "type": "object",
            "description": (
                "STEPWISE DIRECTOR tool gate for the NEXT step. allow: if non-empty, ONLY these tool "
                "names may run; deny: these tool names are blocked. Use to keep the model in the right "
                "phase -- e.g. research (allow reads/Grep/Glob, deny Edit/Write), implement (allow "
                "Edit/Write/Bash), verify (allow Bash). Read/Grep/Glob/WebSearch/WebFetch are always "
                "reachable regardless, so never rely on denying them. Leave both arrays empty to "
                "impose no restriction this step."
            ),
            "properties": {
                "allow": {"type": "array", "items": {"type": "string"}},
                "deny": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["allow", "deny"],
            "additionalProperties": False,
        },
    },
    "required": ["verdict", "steering", "claim", "load_bearing", "verify", "directive", "tool_scope"],
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
                "breaker forbids mutating Bash); for in-repo version/release conventions, read "
                "AGENTS.md, justfile, or scripts/bump_version.py -- not external SemVer docs; "
                "for an external/platform/API claim, missing "
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
        "load_bearing",
        "grounded",
        "needed",
        "provisional_release",
        "lift_reason",
        "lift_scope",
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
    "(its own statements AND the tool output it has seen). "
    "Do NOT arm a claim that already has event=DISARM or event=FAIL_OPEN in prior breaker records. "
    "ARM ONLY when ALL hold: (1) load-bearing for the immediate next action, "
    "(2) asserted as settled without backing evidence, (3) no tool output in the transcript supports it. "
    "DO NOT arm: narration, background speculation, hypotheses being tested, claims about the harness "
    "itself (spec status, hook messages, breaker state, task board), claims paraphrasing a loaded Skill, "
    "or claims already backed by tool output in the transcript. "
    "EXTERNAL/PLATFORM claims: grounded by docs, community prior art (GitHub/issues/gists), or "
    "empirical probe output in the transcript. Do not require official docs when prior art or "
    "empirical proof exists. Do not demand repo files for external truth. "
    "When arming: verdict=1, name the claim, write a steering prompt restricting tools to read-only "
    "(Read, WebSearch, WebFetch, Grep, Glob) and whitelisted research Bash until grounded. "
    "Steer repo claims toward files to read or allowed inspection commands (rg, head, wc), "
    "never blocked commands. "
    "Otherwise verdict=0, steering and claim MUST be empty. "
    "SEPARATELY, on EVERY call (whatever the verdict), act as a stepwise director: write a "
    "terse `directive` naming the single best next action toward the goal, and a `tool_scope` "
    "(allow/deny tool names) keeping the model in the right phase for that action. The directive "
    "and tool_scope are independent of the arm verdict. Call the function exactly once."
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
    "whitelisted research Bash output in the FRESH TOOL block (rg matches, head/wc counts, "
    "git diff/show text) when that output directly proves the claim; also including "
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


def is_release_tool(tool_name: str, input_data: dict | None = None) -> bool:
    tool = str(tool_name or "")
    if tool in RELEASE_TOOLS:
        return True
    if not isinstance(input_data, dict):
        return False
    try:
        from bash_classify import is_allowed_research_bash
        from parse_tool_result import (
            _REPL_BASH_CMD_RE,
            _REPL_CAT_RE,
            _REPL_READ_PATH_RE,
            command_from_input,
            is_repl_tool,
            is_shell_tool,
            repl_code_from_input,
        )
    except ImportError:
        from scripts.gate.bash_classify import is_allowed_research_bash  # pragma: no cover
        from scripts.gate.parse_tool_result import (  # pragma: no cover
            _REPL_BASH_CMD_RE,
            _REPL_CAT_RE,
            _REPL_READ_PATH_RE,
            command_from_input,
            is_repl_tool,
            is_shell_tool,
            repl_code_from_input,
        )
    if is_repl_tool(tool):
        code = repl_code_from_input(input_data)
        bash_cmds = [m.group(1) for m in _REPL_BASH_CMD_RE.finditer(code)]
        if bash_cmds:
            return all(is_allowed_research_bash(cmd)[0] for cmd in bash_cmds)
        return bool(_REPL_READ_PATH_RE.search(code) or _REPL_CAT_RE.search(code))
    if is_shell_tool(tool):
        allowed, _ = is_allowed_research_bash(command_from_input(input_data))
        return allowed
    return False


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("/", "-").replace("_", "-")


def locate_transcript(input_data: dict) -> str | None:
    try:
        from transcript_locate import locate_transcript as _locate
    except ImportError:
        from scripts.gate.transcript_locate import locate_transcript as _locate  # pragma: no cover
    return _locate(input_data)


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
    """Merged judge input: transcript tail + breaker events + spec board + fresh tool.

    Ordered for prompt caching: the big append-only host transcript comes FIRST as
    the stable, cacheable prefix; the small volatile records (breaker events, spec
    board, fresh tool output) are reserved at the END so they cannot shift the
    cached prefix and so tail truncation never drops authoritative task status.
    The host transcript is bounded with a sticky retention window (not a sliding
    `[-n:]`) so its prefix stays byte-identical across same-session judge calls.
    """
    from transcript_tail import MAX_CHARS_PER_TOKEN, retention_window

    tail_parts: list[str] = []
    rendered = render_events(events)
    if rendered:
        tail_parts.append(rendered.rstrip())
    board = _spec_board_block(input_data)
    if board:
        tail_parts.append(board.rstrip())
    if fresh_tool and fresh_tool.strip():
        tail_parts.append('<record line="000000" type="fresh_tool" role="tool">\n' + fresh_tool.strip() + "\n</record>")

    reserve_chars = sum(len(p) + 2 for p in tail_parts)
    host_budget_chars = max(
        2000,
        (max_tokens * MAX_CHARS_PER_TOKEN) - reserve_chars,
    )
    parts: list[str] = []
    host = transcript_segment(input_data, max_tokens=max_tokens)
    if host:
        host = retention_window(host, host_budget_chars)
        parts.append(host.rstrip())
    parts.extend(tail_parts)

    if not parts:
        return ""
    combined = "\n\n".join(parts)
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
    from judge_transport import ask_structured

    return ask_structured(system, user, schema, schema_name="groundedness")


def judge_segment(segment: str, judge: JudgeFn | None = None) -> tuple[int, str]:
    verdict, steering, _claim = arm_judge(segment, events=[], judge=judge)
    return verdict, steering


# --- Predicate self-verify (Approach A) -------------------------------------
# The arm judge may emit a falsifiable predicate over repo files alongside its
# verdict. The breaker runs it READ-ONLY and downgrades an arm to allow ONLY when
# the files CONFIRM the claim. De-escalation only: a refuted or unverifiable
# predicate leaves the verdict unchanged, so a buggy or empty predicate can never
# introduce a new block -- only remove a false one. Fail-safe: any error returns
# "unverifiable" (no downgrade).
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


def _provisional_lift_message(reason: str, scope: str) -> str:
    return (
        f"unifable breaker: provisional lift — {reason} "
        f"Stay within scope: {scope}. Mutations allowed until grounded; minor drift yields "
        "advisory hints only."
    )


def _disarm_message() -> str:
    return "unifable breaker open: the flagged claim is grounded. Write/Edit/Bash are unrestricted again."


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


def should_coalesce(state: dict, key: str, now: float, window: float = JUDGE_COALESCE_WINDOW_SECONDS) -> bool:
    """True when a judge already fired for this key within the coalesce window.

    Used by the locked wrapper to mark later calls of the same parallel batch so
    they skip their (redundant) judge call. Requires a key match so a stale arm
    from a different prompt never suppresses a fresh judge."""
    if state.get("breaker_key") != key:
        return False
    last = state.get("breaker_judge_call_at") or 0.0
    if not last:
        return False
    try:
        # abs(): sibling processes of one batch capture time.time() independently,
        # and the first to take the lock may not hold the earliest stamp -- a few-ms
        # negative delta is still the same batch, so coalesce on proximity either way.
        return abs(now - float(last)) < window
    except (TypeError, ValueError):
        return False


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
        sys.stderr.write(f"[unifable breaker] auto-released after {count} consecutive blocks (fail-open)\n")
    except Exception:
        pass


def evaluate_pre_tool(
    input_data: dict,
    state: dict,
    now: float,
    active_task: str,
    judge: JudgeFn | None = None,
    coalesce: bool = False,
) -> tuple[bool, str, str]:
    """PreToolUse path: arm judge (debounced) and block mutation tools while armed.

    When `coalesce` is True the imminent call belongs to a parallel batch that a
    sibling process already judged: every judge call is skipped and the block
    decision falls through to the already-persisted breaker state, so the batch
    costs one judge call, not N. The locked wrapper (evaluate_pre_tool_locked) is
    the only caller that sets it; direct callers keep the original behavior."""
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
            if claim and segment.strip() and not coalesce:
                state["breaker_judge_call_at"] = now
                release_verdict = disarm_judge(claim, segment, user_goal=user_goal, judge=judge)
                disarmed, lift_msg = _apply_release(state, claim, release_verdict)
                if disarmed:
                    provisional = False
                    state["breaker_pending_notify"] = _disarm_message()
                elif lift_msg:
                    state["breaker_pending_notify"] = lift_msg
            if state.get("breaker_provisional") and is_mutation_tool(tool):
                scope = str(state.get("breaker_lift_scope") or "")
                if claim and scope and not coalesce:
                    state["breaker_judge_call_at"] = now
                    drift, feedback = monitor_provisional_judge(
                        claim,
                        scope,
                        segment,
                        tool,
                        user_goal=user_goal,
                        judge=judge,
                    )
                    if drift == 2:
                        append_event(state, "REINSTATE", claim=claim, corrective=feedback)
                        reinstate(state, claim, feedback or "Return to the verification scope.")
                        return True, feedback or "Return to the verification scope.", ""
                    if drift == 1 and feedback:
                        append_event(state, "SCOPE_HINT", claim=claim, hint=feedback)
                        hint_msg = f"{_SCOPE_HINT_PREFIX}{feedback}"
                        existing = str(state.get("breaker_pending_notify") or "")
                        state["breaker_pending_notify"] = f"{existing}\n{hint_msg}".strip() if existing else hint_msg
            pending = str(state.get("breaker_pending_notify") or "")
            if pending:
                state["breaker_pending_notify"] = ""
                notify_out = pending
            return False, "", notify_out
        if not armed and not coalesce and should_judge(state, key, now):
            segment = judge_transcript(input_data, events)
            state["breaker_judge_call_at"] = now
            director_out: dict[str, Any] = {}
            verdict, steering, claim = arm_judge(
                segment, events=events, judge=judge, input_data=input_data, out=director_out
            )
            if verdict == 1 and claim and claim_already_adjudicated(claim, events):
                verdict, steering, claim = 0, "", ""
            record_verdict(state, key, now, verdict, steering, claim)
            # Stepwise director: persist directive + scope from this (debounced)
            # call. When arming, the breaker owns the block, so clear the scope and
            # let steering carry the instruction. When disarmed, enforce the scope
            # and surface the directive on the allow path (~once per debounce window).
            if verdict == 1:
                state["breaker_directive"] = ""
                state["breaker_tool_scope"] = {}
                state["breaker_last_directive_surfaced"] = ""
            else:
                directive = str(director_out.get("directive") or "")
                scope = director_out.get("tool_scope")
                state["breaker_directive"] = directive
                state["breaker_tool_scope"] = scope if isinstance(scope, dict) else {}
                # Surface only a CHANGED directive, so steady work on one step does
                # not re-emit the same line every debounce window (token-aware).
                if directive and directive != str(state.get("breaker_last_directive_surfaced") or ""):
                    msg = f"unifable director: {directive}"
                    existing = str(state.get("breaker_pending_notify") or "")
                    state["breaker_pending_notify"] = f"{existing}\n{msg}".strip() if existing else msg
                    state["breaker_last_directive_surfaced"] = directive
        elif armed:
            claim = str(state.get("breaker_claim") or "")
            if claim and not coalesce:
                state["breaker_judge_call_at"] = now
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
        if not coalesce:
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


def evaluate_pre_tool_locked(
    input_data: dict,
    now: float,
    active_task: str,
    judge: JudgeFn | None = None,
    timeout: float | None = None,
) -> tuple[bool, str, str, dict]:
    """Load -> evaluate -> save the breaker under a cross-process lock.

    This is the hook entrypoint. The lock serializes the concurrent PreToolUse
    processes of a parallel tool-call batch: the first to acquire it judges and
    persists its verdict; the rest then load that fresh state, see a judge already
    fired within the coalesce window, and skip their own (identical) judge call.
    Result: one judge API call per batch instead of N, with blocking semantics
    unchanged. Fail-open: breaker_lock runs the body unlocked if it cannot lock,
    and any error leaves the tool unblocked. Returns the state too so the caller
    can inspect the event log (e.g. for REINSTATE)."""
    with breaker_lock(input_data, timeout):
        state = load_breaker(input_data)
        key = breaker_key(str(input_data.get("session_id") or ""), str(active_task or ""))
        coalesce = should_coalesce(state, key, now)
        block, steering, notify = evaluate_pre_tool(input_data, state, now, active_task, judge=judge, coalesce=coalesce)
        save_breaker(input_data, state)
    return block, steering, notify, state


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
    if not is_release_tool(tool, input_data):
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
