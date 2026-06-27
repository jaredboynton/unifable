#!/usr/bin/env python3
"""Groundedness-breaker text filters: harness self-reference, task-board status,
repo-path hypotheses, loaded-skill paraphrase, and spec-board claim support.

Pure, dependency-light predicates over transcript text. Extracted from
groundedness.py; re-exported by the groundedness facade.
"""
from __future__ import annotations

import json
import re
from typing import Any

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
    try:
        from breaker_runtime import RELEASE_TOOLS
    except ImportError:  # pragma: no cover
        from scripts.gate.breaker_runtime import RELEASE_TOOLS
    if tool not in RELEASE_TOOLS:
        try:
            from parse_tool_result import _mcp_tool_is_read_like, is_mcp_tool, read_targets
        except ImportError:
            from scripts.gate.parse_tool_result import (  # pragma: no cover
                _mcp_tool_is_read_like,
                is_mcp_tool,
                read_targets,
            )
        if is_mcp_tool(tool) and _mcp_tool_is_read_like(tool):
            paths = read_targets(input_data)
            return paths[0] if paths else ""
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


_SKILL_TOOL_USE_LEGACY_RE = re.compile(r"\[tool_use name=Skill\][^\n]*\n([^\n]+)")
_SKILL_TOOL_USE_RE = _SKILL_TOOL_USE_LEGACY_RE
_SKILL_TOOL_USE_PATCHPRESS_RE = re.compile(
    r"@@tool Skill[^\n]*\n(\{[\s\S]*?\})\n(?:stats:|\[tool_result\]|</record>|@@tool |\Z)"
)


_QUOTED_VALUE_RE = re.compile(r'"([^"\\]+)"')


def _skill_names_from_tool_block(block: str) -> set[str]:
    names: set[str] = set()
    line = block.strip()
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


def loaded_skill_names(segment: str) -> set[str]:
    """Skill names loaded via the Skill tool in the transcript segment."""
    names: set[str] = set()
    text = str(segment or "")
    for m in _SKILL_TOOL_USE_LEGACY_RE.finditer(text):
        names.update(_skill_names_from_tool_block(m.group(1)))
    for m in _SKILL_TOOL_USE_PATCHPRESS_RE.finditer(text):
        names.update(_skill_names_from_tool_block(m.group(1)))
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
