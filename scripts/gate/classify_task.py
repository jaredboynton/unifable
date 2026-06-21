#!/usr/bin/env python3
"""Task-mode classification for the unifable observation gate (bilingual KO/EN).

Trimmed port of fable-ish: only prompt classification (quick / normal / deep)
and the per-mode context nudge are kept. Danger/secret command blocking is the
host harness's job (e.g. block_dangerous.py), so it is intentionally dropped.
"""

from __future__ import annotations

import re


QUICK_RE = re.compile(
    r"(?i)\b(quick|brief|briefly|simple|simply|just explain|explain only|review only|direction|"
    r"check only|no edits|do not edit)\b|간단히|빠르게|설명만|검토만|방향|확인만"
)
DEEP_RE = re.compile(
    r"(?i)\b(deep|thorough|thoroughly|exhaustive|end-to-end|production-ready|deploy|deployment|"
    r"migration|database|auth|security|refactor|large|complex|implement the plan)\b|"
    r"끝까지|철저|전부|전체|배포|마이그레이션|인증|보안|리팩터"
)
NORMAL_RE = re.compile(
    r"(?i)\b(implement|fix|debug|change|edit|create|build|test|lint|review|update)\b|"
    r"구현|수정|고쳐|디버그|작성|생성|테스트|검증"
)
# Hedging / uncertainty cues. High-precision only: bare "should" is excluded
# (it is usually imperative — "you should add a test" — not doubt), so only
# clear modal-uncertainty phrases like "should probably" are matched.
AMBIGUOUS_RE = re.compile(
    r"(?i)(\b(probably|maybe|perhaps|might|i think|i guess|i wonder|unsure|uncertain|"
    r"unclear|could be|might be|should probably|hard to say|is it possible|"
    r"not sure|not quite sure|not entirely sure|not certain|i'm not sure)\b)|"
    r"아마|잘 모르|모르겠|확실하지 않|불확실|애매|긴가민가"
)


def classify_prompt(prompt: str) -> tuple[str, list[str]]:
    text = prompt or ""
    lowered = text.lower()
    risks: list[str] = []
    if "production" in lowered or "배포" in text:
        risks.append("production")
    if re.search(r"(?i)\b(db|database|migration|migrate|schema)\b|데이터베이스|마이그레이션", text):
        risks.append("database")
    if re.search(r"(?i)\b(auth|secret|token|api[_ -]?key|password)\b|인증|비밀|토큰", text):
        risks.append("secret-or-auth")
    if re.search(r"(?i)\b(git\s+push|release|publish)\b|릴리즈|배포", text):
        risks.append("remote-write")

    # Hedging language signals the user lacks a confident answer — which calls for
    # MORE evidence, not less. It must never land in 'quick' (which waives
    # verification): float a would-be-quick task up to 'normal' and attach an
    # 'uncertainty' flag that triggers a research/grounding nudge. It does NOT
    # force 'deep' (hedging severity varies); genuine deep signals still escalate.
    ambiguous = bool(AMBIGUOUS_RE.search(text))
    if ambiguous:
        risks.append("uncertainty")
    hard_risks = [r for r in risks if r != "uncertainty"]

    if DEEP_RE.search(text) or any(flag in hard_risks for flag in ("production", "database", "remote-write")):
        return "deep", risks
    if QUICK_RE.search(text) and not hard_risks:
        return "quick", risks
    if NORMAL_RE.search(text):
        return "normal", risks
    if ambiguous:
        return "normal", risks
    return "quick", risks


# Map the observation-gate mode onto the spec-gate grade tier. quick work is
# LIGHT (spec waived), normal is STANDARD (full spec), deep is HEAVY (adds
# architectural constraints + >=2 rejected alternatives). The mapping itself now
# lives in scripts/gate/evidence_policy.py (the single policy boundary); these are
# back-compat shims so existing importers (hooks, tests) keep working.
try:  # bare import on sys.path (hooks + tests); package import otherwise
    from evidence_policy import MODE_TO_GRADE as GRADE_BY_MODE, grade_for_mode
except ImportError:  # pragma: no cover
    from scripts.gate.evidence_policy import MODE_TO_GRADE as GRADE_BY_MODE, grade_for_mode


def grade_of(mode: str) -> str:
    return grade_for_mode(mode)


def context_for_mode(mode: str, risk_flags: list[str]) -> str:
    lines = [f"unifable gate — task mode: {mode}."]
    if risk_flags:
        lines.append("Risk flags: " + ", ".join(risk_flags) + ".")
    if mode == "quick":
        lines.append("Keep it concise; no forced verification.")
    elif mode == "normal":
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
    lines.append(
        "Cite evidence for load-bearing claims: path:line for code, cmd -> output for tool "
        "results, a URL for research/prior art. Never claim verification not observed in a tool result."
    )
    return "\n".join(lines[:10])
