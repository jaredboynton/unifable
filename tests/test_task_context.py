#!/usr/bin/env python3
"""Tests for scripts/gate/task_context.py."""

from __future__ import annotations

import sys
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE_DIR))

import task_context as tc  # noqa: E402


def test_self_referential_harness_task_detected():
    prompt = (
        "Add a focused regression test proving a benchmark result is not accepted "
        "unless the saved summary includes all four cells."
    )
    assert tc.is_self_referential_harness_task(prompt)
    line = tc.self_referential_harness_context_line(prompt)
    assert "Self-referential harness task" in line
    assert "hooks/" in line
    assert "scripts/gate/" in line


def test_normal_feature_task_not_self_referential():
    prompt = "Add OAuth token refresh to the API client and unit tests."
    assert not tc.is_self_referential_harness_task(prompt)
    assert tc.self_referential_harness_context_line(prompt) == ""
