#!/usr/bin/env python3
"""Tests for the single-purpose judge-backed grade classifier (grade_override.py).

Verifies the schema is clean, parse_grade_verdict coerces bad input safely,
and fail-open returns normal.
Run: python3 -m pytest tests/test_grade_classify_judge.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import grade_override as go  # noqa: E402


# Schema ---------------------------------------------------------------------

def test_schema_is_mode_risk_flags_reason():
    props = go._GRADE_SCHEMA["properties"]
    assert set(props.keys()) == {"mode", "risk_flags", "reason", "evidence_profile"}
    assert go._GRADE_SCHEMA["required"] == ["mode", "risk_flags", "reason", "evidence_profile"]
    assert props["mode"]["enum"] == ["quick", "normal", "deep"]
    assert props["evidence_profile"]["enum"] == ["code", "operational"]


# parse_grade_verdict --------------------------------------------------------

def test_parse_valid_verdict():
    mode, flags, reason, profile = go.parse_grade_verdict(
        {
            "mode": "normal",
            "risk_flags": ["uncertainty"],
            "reason": "hedged",
            "evidence_profile": "operational",
        }
    )
    assert mode == "normal"
    assert flags == ["uncertainty"]
    assert reason == "hedged"
    assert profile == "operational"


def test_parse_operational_deep_coerced_to_normal():
    mode, _, reason, profile = go.parse_grade_verdict(
        {
            "mode": "deep",
            "risk_flags": [],
            "reason": "account research",
            "evidence_profile": "operational",
        }
    )
    assert mode == "normal"
    assert profile == "operational"
    assert "coerced" in reason.lower()


def test_parse_bad_mode_falls_to_normal():
    mode, _, _, profile = go.parse_grade_verdict(
        {"mode": "invalid", "risk_flags": [], "reason": "", "evidence_profile": "code"}
    )
    assert mode == "normal"
    assert profile == "code"


def test_parse_none_verdict_fails_open():
    mode, flags, reason, profile = go.parse_grade_verdict(None)
    assert mode == "normal"
    assert flags == []
    assert reason == ""
    assert profile == "code"


def test_parse_non_list_risk_flags():
    _, flags, _, _ = go.parse_grade_verdict(
        {"mode": "quick", "risk_flags": "oops", "reason": "", "evidence_profile": "code"}
    )
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
        return {"mode": "deep", "risk_flags": [], "reason": "architectural", "evidence_profile": "code"}

    verdict = go.judge_grade_classify("migrate to event-driven", judge_fn=fake)
    assert verdict["mode"] == "deep"
    assert calls == ["migrate to event-driven"]


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
