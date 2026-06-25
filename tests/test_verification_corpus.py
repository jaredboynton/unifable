#!/usr/bin/env python3
"""A recorded verification run (e.g. a pytest the agent already executed) must
reach the evidence_only corpus, so the Stop judge can count it without the agent
laundering the command through a research wrapper to capture its output."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

from parse_tool_result import format_verifications  # noqa: E402
from spec_judge import _evidence_payload  # noqa: E402


def test_format_verifications_maps_status_and_summary():
    records = [
        {"command": "python3 -m pytest tests/test_x.py -q", "success": True, "summary": "1 passed"},
        {"command": "ruff check .", "success": False, "summary": "E501 line too long"},
        {"command": "make build", "success": None, "summary": ""},
    ]
    out = format_verifications(records)
    assert out == [
        "python3 -m pytest tests/test_x.py -q -> PASS: 1 passed",
        "ruff check . -> FAIL: E501 line too long",
        "make build -> RAN",
    ]


def test_format_verifications_is_bounded_and_robust():
    assert format_verifications(None) == []
    assert format_verifications([]) == []
    # Non-dict entries and entries without a command are dropped.
    assert format_verifications(["nope", {"success": True}]) == []
    many = [{"command": f"cmd{i}", "success": True} for i in range(30)]
    out = format_verifications(many, limit=20)
    assert len(out) == 20
    assert out[0] == "cmd10 -> PASS"  # keeps the most recent 20


def test_evidence_payload_includes_verifications():
    payload = _evidence_payload(
        {"verifications": ["python3 -m pytest tests/test_x.py -q -> PASS: 1 passed"]}
    )
    assert payload is not None
    assert payload["verifications"] == ["python3 -m pytest tests/test_x.py -q -> PASS: 1 passed"]


def test_evidence_payload_verifications_alone_is_a_nonempty_corpus():
    # A run whose only captured proof is a passing verification must still produce
    # a corpus (not None), or the evidence_only judge has nothing to adjudicate.
    payload = _evidence_payload({"verifications": ["pytest -q -> PASS: 5 passed"]})
    assert payload is not None and any(payload.values())
