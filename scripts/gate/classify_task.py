#!/usr/bin/env python3
"""Support functions for the judge-backed grade classifier.

The deterministic word-match classifier (QUICK_RE / DEEP_RE / NORMAL_RE) has been
replaced by a single-purpose gpt-realtime-2 judge in grade_override.py
(judge_grade_classify). This module retains the support functions the hooks still
need: operative_prompt (extract the user's actual instruction from pasted corpus)
and context_for_mode (the per-mode nudge text).

Danger/secret command blocking is the host harness's job, so it is intentionally
absent.
"""

from __future__ import annotations

_OPERATIVE_MAX_CHARS = 4096
_USER_TURN_MARKERS = ("\n❯ ", "\n> ", "\nuser:", "\nUSER:")


def operative_prompt(prompt: str, *, max_chars: int = _OPERATIVE_MAX_CHARS) -> str:
    """Return the user's operative instruction, not pasted corpus/tool output.

    Prefer text after the final user-turn marker; otherwise the trailing slice of
    the prompt. The grade classifier runs on this slice only."""
    text = (prompt or "").strip()
    if not text:
        return ""
    chunk = text
    for marker in _USER_TURN_MARKERS:
        idx = text.rfind(marker)
        if idx >= 0:
            chunk = text[idx + len(marker) :].strip()
            break
    if len(chunk) > max_chars:
        chunk = chunk[-max_chars:]
    return chunk


# Map the observation-gate mode onto the spec-gate grade tier. quick work is
# LIGHT (spec waived), normal is STANDARD (full spec), deep is HEAVY (adds
# architectural constraints + >=2 rejected alternatives). The mapping itself now
# lives in scripts/gate/evidence_policy.py; HEAVY uses frontier-first workflow.
try:  # bare import on sys.path (hooks + tests); package import otherwise
    from evidence_policy import grade_for_mode
except ImportError:  # pragma: no cover
    from scripts.gate.evidence_policy import grade_for_mode


def grade_of(mode: str) -> str:
    return grade_for_mode(mode)


def context_for_mode(
    mode: str,
    risk_flags: list[str],
    *,
    first_prompt: bool = True,
) -> str:
    lines: list[str] = []
    if risk_flags:
        # "uncertainty" gets its own actionable paragraph below, so don't also
        # name it in the bare enumeration -- that states the same signal twice.
        shown = [f for f in risk_flags if f != "uncertainty"]
        if shown:
            lines.append("Risk flags: " + ", ".join(shown) + ".")
    if mode == "normal":
        lines.append("If files change, run one relevant verification command or state why none applies.")
    elif mode == "deep":
        lines.append(
            "Define the exit proof before completion and verify changed behavior before final. "
            "If you verified a change or your claims rest on tool results, state the evidence "
            "(and any gaps) in one line; if nothing changed and there is nothing to verify, "
            "skip the verification note."
        )
    if "uncertainty" in risk_flags:
        lines.append(
            "The prompt hedges (uncertain). Treat it as a research task: gather evidence and "
            "confirm with tool calls before answering; do not guess. State what you verified and "
            "what is still unknown."
        )
    if first_prompt:
        lines.append(
            "Cite evidence for load-bearing claims: path:line for code, cmd -> output for tool "
            "results, a URL for research/prior art. Never claim verification not observed in a tool result."
        )
    return "\n".join(lines[:10])
