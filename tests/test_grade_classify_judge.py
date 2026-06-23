#!/usr/bin/env python3
"""Tests for the single-purpose judge-backed grade classifier (grade_override.py).

Verifies the system prompt instructs the judge correctly, the schema is clean,
parse_grade_verdict coerces bad input safely, and fail-open returns normal.
Run: python3 -m pytest tests/test_grade_classify_judge.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import grade_override as go  # noqa: E402


# System prompt content ------------------------------------------------------

def test_system_prompt_describes_three_modes():
    s = go._GRADE_SYSTEM.lower()
    assert "quick" in s and "light" in s
    assert "normal" in s and "standard" in s
    assert "deep" in s and "heavy" in s


def test_system_prompt_has_auth_code_is_normal_rule():
    """The core fix: touching auth/security/production code on a bounded plan is
    NORMAL, not DEEP."""
    s = go._GRADE_SYSTEM.lower()
    assert "auth" in s
    assert "normal, not deep" in s


def test_system_prompt_has_hedging_rule():
    s = go._GRADE_SYSTEM.lower()
    assert "hedging" in s or "hedge" in s
    assert "uncertainty" in s


def test_system_prompt_prefers_normal_on_ambiguity():
    s = go._GRADE_SYSTEM.lower()
    assert "prefer normal over deep" in s


# Schema ---------------------------------------------------------------------

def test_schema_is_mode_risk_flags_reason():
    props = go._GRADE_SCHEMA["properties"]
    assert set(props.keys()) == {"mode", "risk_flags", "reason"}
    assert go._GRADE_SCHEMA["required"] == ["mode", "risk_flags", "reason"]
    assert props["mode"]["enum"] == ["quick", "normal", "deep"]


# parse_grade_verdict --------------------------------------------------------

def test_parse_valid_verdict():
    mode, flags, reason = go.parse_grade_verdict(
        {"mode": "normal", "risk_flags": ["uncertainty"], "reason": "hedged"}
    )
    assert mode == "normal"
    assert flags == ["uncertainty"]
    assert reason == "hedged"


def test_parse_bad_mode_falls_to_normal():
    mode, _, _ = go.parse_grade_verdict({"mode": "invalid", "risk_flags": [], "reason": ""})
    assert mode == "normal"


def test_parse_none_verdict_fails_open():
    mode, flags, reason = go.parse_grade_verdict(None)
    assert mode == "normal"
    assert flags == []
    assert reason == ""


def test_parse_non_list_risk_flags():
    _, flags, _ = go.parse_grade_verdict({"mode": "quick", "risk_flags": "oops", "reason": ""})
    assert flags == []


# judge_grade_classify fail-open ---------------------------------------------

def test_judge_returns_none_on_empty_operative():
    assert go.judge_grade_classify("") is None
    assert go.judge_grade_classify("   ") is None


def test_judge_fail_open_on_transport_error():
    def boom(**kw):
        raise RuntimeError("transport down")
    assert go.judge_grade_classify("fix the bug", judge_fn=boom) is None


def test_judge_uses_injected_fn():
    calls = []

    def fake(operative, **kw):
        calls.append(operative)
        return {"mode": "deep", "risk_flags": [], "reason": "architectural"}

    verdict = go.judge_grade_classify("migrate to event-driven", judge_fn=fake)
    assert verdict["mode"] == "deep"
    assert calls == ["migrate to event-driven"]


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
