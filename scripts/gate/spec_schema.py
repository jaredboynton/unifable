#!/usr/bin/env python3
"""Spec schema, fake-evidence markers, and citation-format helpers (unifable).

Lowest layer of the evidence-spec gate: pure data plus stdlib-only predicates with
no sibling gate imports. Re-exported by the spec.py facade; imported directly by the
other spec_* sub-modules.
"""

from __future__ import annotations

import re
from typing import Any

SPEC_SCHEMA: dict[str, dict[str, Any]] = {
    # required
    "restated_goal": {
        "type": str,
        "required": True,
        "description": "The goal restated in the model's own words; must differ from raw ask.",
    },
    "acceptance_criteria": {
        "type": list,
        "required": True,
        "description": "List of {check: <runnable command str>, evidence: <observed output>}.",
    },
    # optional
    "risks": {
        "type": list,
        "required": False,
        "description": "List of risks with blast-radius and mitigation.",
    },
    "non_goals": {
        "type": list,
        "required": False,
        "description": "What is explicitly out of scope.",
    },
    # evidence-gate citation fields (required only when require_evidence=True)
    "repo_context": {
        "type": list,
        "required": False,
        "description": "CODE evidence: 'path:line' citations the model actually read before deciding.",
    },
    "prior_art": {
        "type": list,
        "required": False,
        "description": "RESEARCH evidence: each {cite: 'http(s)://...', why: '<why it backs the approach>'} (docs/repos/papers).",
    },
    "evidence_profile": {
        "type": str,
        "required": False,
        "description": "code | operational — set by grade classifier; operational waives repo_context/prior_art at STANDARD+.",
    },
    # CLI-managed task list. Each task carries a runnable `check`; a task becomes
    # `validated` only when the check runs AND the codex judge confirms the output
    # actually satisfies it. When a spec declares tasks, completion (Stop gate)
    # requires EVERY task validated. Authored and mutated only via spec.py CLI.
    "tasks": {
        "type": list,
        "required": False,
        "description": "List of {id, title, check, status, exit, output, judge_verdict, judge_reason}.",
    },
}


GRADES = ("LIGHT", "STANDARD", "HEAVY")


FAKE_MARKERS: tuple[str, ...] = (
    "not run",
    "assumed",
    "assumption",
    "(assumption)",
    "i assume",
    "presumably",
    "would pass",
    "will pass",
    "should pass",
    "tbd",
    "pending",
    "n/a",
    "todo",
    "will run",
    "placeholder",
    "to be determined",
    "not tested",
    "not verified",
    "not checked",
    "skipped",
    "manually verified",
    "manually tested",
    "trust me",
    "obviously works",
)


def check_fake_evidence(text: str) -> list[str]:
    """Return any FAKE_MARKERS found (case-insensitive) in *text*.

    Used to reject acceptance_criteria evidence fields that contain placeholder
    language rather than live command output.
    """
    lower = (text or "").lower()
    return [marker for marker in FAKE_MARKERS if marker in lower]


_PATH_LINE_RE = re.compile(r"^.+:\d+(?:-\d+)?$")


_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def is_path_line(s: str) -> bool:
    """True when *s* looks like a 'path:line' code citation (not a URL)."""
    if not isinstance(s, str):
        return False
    s = s.strip()
    if s.lower().startswith(("http://", "https://")):
        return False
    return bool(_PATH_LINE_RE.match(s))


def is_source_url(s: str) -> bool:
    """True when *s* is an http(s) URL."""
    return isinstance(s, str) and bool(_URL_RE.match(s.strip()))


def repo_context_parts(item: Any) -> tuple[str, str]:
    """Return (cite, why) for a repo_context entry.

    Accepts the required object form {'cite': 'path:line', 'why': '<why relevant>'}.
    A bare 'path:line' string yields (string, '') so the missing-why check fires."""
    if isinstance(item, dict):
        return str(item.get("cite") or item.get("path") or ""), str(item.get("why") or "")
    if isinstance(item, str):
        return item, ""
    return "", ""


def repo_context_of(spec: dict[str, Any]) -> list:
    """Return the spec's repo_context list, falling back to the legacy `must_read`
    key. The field was renamed `must_read` -> `repo_context`; a spec authored under
    the old name (an on-disk spec predating the rename, or a session whose gate
    upgraded mid-flight) must still resolve, or the upgrade strands it: every edit
    is blocked and Stop is blocked, with no in-session way to rewrite the protected
    spec. New specs always write `repo_context`; this is read-side back-compat only.
    Returns the first non-empty list among (repo_context, must_read), else []."""
    for key in ("repo_context", "must_read"):
        val = spec.get(key)
        if isinstance(val, list) and val:
            return val
    return []


def prior_art_parts(item: Any) -> tuple[str, str]:
    """Return (cite, why) for a prior_art entry.

    Accepts the required object form {'cite': 'http(s)://...', 'why': '<why relevant>'}.
    A bare URL string yields (url, '') so the missing-why check fires."""
    if isinstance(item, dict):
        return str(item.get("cite") or item.get("url") or ""), str(item.get("why") or "")
    if isinstance(item, str):
        return item, ""
    return "", ""


def spec_template() -> dict[str, Any]:
    """Return an empty spec scaffold the model can fill in."""
    return {
        "restated_goal": "",
        "acceptance_criteria": [{"check": "", "evidence": ""}],
        "repo_context": [{"cite": "", "why": ""}],
        "prior_art": [],
        "evidence_profile": "code",
        "risks": [],
        "non_goals": [],
        "heavy_workflow": False,
    }
