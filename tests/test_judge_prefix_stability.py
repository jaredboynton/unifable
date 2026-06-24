#!/usr/bin/env python3
"""Prefix stability for prompt caching: the arm-judge system prompt MUST be a
byte-identical, cacheable prefix across calls. The volatile adjudicated-claims
list rides the END of the user message, after the append-only transcript.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import groundedness as gb  # noqa: E402
from breaker_state import append_event, default_breaker  # noqa: E402


class RecordingJudge:
    def __init__(self):
        self.systems: list[str] = []
        self.users: list[str] = []

    def __call__(self, system, user, schema):
        self.systems.append(system)
        self.users.append(user)
        return {"verdict": 0, "steering": "", "claim": "", "load_bearing": 0}


def test_arm_judge_system_is_stable_constant():
    j = RecordingJudge()
    seg = '<record line="000001" type="assistant">the bug is in foo</record>'
    gb.arm_judge(seg, events=[], judge=j)

    st = default_breaker()
    append_event(st, "DISARM", claim="claim A", grounded=True)
    append_event(st, "DISARM", claim="claim B", grounded=True)
    gb.arm_judge(seg, events=st["events"], judge=j)

    assert j.systems[0] == gb._JUDGE_SYSTEM
    assert j.systems[1] == gb._JUDGE_SYSTEM
    assert j.systems[0] == j.systems[1]  # identical cacheable prefix regardless of claims


def test_adjudicated_claims_ride_user_suffix():
    j = RecordingJudge()
    st = default_breaker()
    append_event(st, "DISARM", claim="already grounded claim", grounded=True)
    gb.arm_judge("SEGMENT-BODY", events=st["events"], judge=j)

    user = j.users[0]
    assert user.startswith("SEGMENT-BODY")  # transcript first = stable prefix
    assert "ALREADY ADJUDICATED" in user
    assert "already grounded claim" in user  # claims appended AFTER the transcript


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
