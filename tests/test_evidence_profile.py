#!/usr/bin/env python3
"""Evidence profile resolution and judge classification fixtures."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import grade_override as go  # noqa: E402
from evidence_policy import resolve_evidence_profile  # noqa: E402


def test_resolve_evidence_profile_prefers_spec_over_ledger():
    spec = {"evidence_profile": "operational"}
    ledger = {"evidence_profile": "code"}
    assert resolve_evidence_profile(ledger, spec) == "operational"


def test_resolve_evidence_profile_defaults_to_code():
    assert resolve_evidence_profile({}, None) == "code"
    assert resolve_evidence_profile({"evidence_profile": "bogus"}, {}) == "code"


def test_judge_classifies_nrg_like_prompt_operational():
    captured: dict = {}

    def fake_ask(system, user, schema, **kw):
        captured["user"] = user
        return {
            "mode": "normal",
            "risk_flags": [],
            "reason": "internal account research and reply drafting",
            "evidence_profile": "operational",
        }

    prompt = "research NRG account across Salesforce/Slack/Gong and draft a reply to Bill"
    with patch("grade_override.ask_structured", fake_ask, create=True):
        with patch("codex_judge.ask_structured", fake_ask):
            verdict = go.judge_grade_classify(
                prompt,
                judge_fn=lambda operative, **kw: {
                    "mode": "normal",
                    "risk_flags": [],
                    "reason": "internal account research and reply drafting",
                    "evidence_profile": "operational",
                },
            )
    mode, _, _, profile = go.parse_grade_verdict(verdict)
    assert mode == "normal"
    assert profile == "operational"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
