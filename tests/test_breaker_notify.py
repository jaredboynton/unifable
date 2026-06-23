#!/usr/bin/env python3
"""Notification-surface tests: every judge/breaker state transition must keep the
main model up to date.

These cover the previously-silent transitions:
  N1  PreToolUse DISARM  -> "breaker open" notify (was silent; only PostToolUse spoke)
  N2  PreToolUse NEEDED  -> "still armed" + needed notify on a read while armed
  N3  FAIL_OPEN (cap)    -> model-facing "auto-released / fail-open" notify
  N4  STALE_ARM_DROPPED  -> "stale arm cleared" notify on a new prompt/session
  N5  evidence gate blocking a mutation must still surface a pending breaker notify

Run: python3 -m pytest tests/test_breaker_notify.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import groundedness as gb  # noqa: E402
from breaker_state import default_breaker  # noqa: E402


class _Judge:
    """Routes by system prompt: arm judge vs release monitor vs lift monitor."""

    def __init__(self, arm=(1, "blocked", "claim X"), grounded=1, needed=""):
        self.arm = arm
        self.grounded = grounded
        self.needed = needed
        self.arm_calls = 0
        self.disarm_calls = 0

    def __call__(self, system, user, schema):
        s = system.lower()
        if "provisional-lift monitor" in s:
            return {"drift_level": 0, "hint": "", "corrective": ""}
        if "release monitor" in s:
            self.disarm_calls += 1
            if self.grounded:
                return {
                    "grounded": 1, "needed": "", "load_bearing": 1,
                    "provisional_release": 0, "lift_reason": "", "lift_scope": "",
                }
            return {
                "grounded": 0, "needed": self.needed, "load_bearing": 1,
                "provisional_release": 0, "lift_reason": "", "lift_scope": "",
            }
        self.arm_calls += 1
        v, st, c = self.arm
        return {"verdict": v, "steering": st, "claim": c, "load_bearing": 1 if v == 1 else 0}


def _pre(tool, session="S"):
    return {"tool_name": tool, "session_id": session, "cwd": "/repo"}


# N1 -------------------------------------------------------------------------
def test_pre_tool_disarm_emits_breaker_open_notify(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "cited the source file")
    state = default_breaker()
    gb.arm(state, gb.breaker_key("S", "P"), 0.0, "read X and cite it", "claim X")
    judge = _Judge(grounded=1)
    blocked, steering, notify = gb.evaluate_pre_tool(
        _pre("Edit"), state, now=1.0, active_task="P", judge=judge
    )
    assert blocked is False
    assert state["breaker_armed"] is False
    assert "breaker open" in notify.lower()


# N2 -------------------------------------------------------------------------
def test_pre_tool_needed_emits_still_armed_notify_on_read(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "read the wrong file")
    state = default_breaker()
    gb.arm(state, gb.breaker_key("S", "P"), 0.0, "read X", "claim X")
    judge = _Judge(grounded=0, needed="read foo.py:10 and cite the constant")
    blocked, steering, notify = gb.evaluate_pre_tool(
        _pre("Read"), state, now=1.0, active_task="P", judge=judge
    )
    assert blocked is False  # Read is never blocked
    assert state["breaker_armed"] is True
    assert "still armed" in notify.lower()
    assert "read foo.py:10" in notify


# N3 -------------------------------------------------------------------------
def test_fail_open_emits_auto_released_notify(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    monkeypatch.setenv("UNIFABLE_BREAKER_MAX_BLOCKS", "3")
    judge = _Judge(arm=(1, "blocked", "uncapped claim"), grounded=0)
    state = default_breaker()
    b1, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    b2, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    b3, steering3, notify3 = gb.evaluate_pre_tool(
        _pre("Edit"), state, now=2.0, active_task="P", judge=judge
    )
    assert b1 is True and b2 is True and b3 is False
    assert state["breaker_armed"] is False
    assert "fail-open" in notify3.lower() or "auto-released" in notify3.lower()


# N4 -------------------------------------------------------------------------
def test_stale_arm_dropped_emits_notify(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")
    state = default_breaker()
    gb.arm(state, gb.breaker_key("S", "P1"), 0.0, "blocked", "claim X")
    judge = _Judge(arm=(0, "", ""))
    blocked, steering, notify = gb.evaluate_pre_tool(
        _pre("Read", session="S"), state, now=1.0, active_task="P2", judge=judge
    )
    assert blocked is False
    assert state["breaker_armed"] is False
    assert "stale" in notify.lower()


# N5 -------------------------------------------------------------------------
def test_evidence_gate_block_still_surfaces_breaker_notify(monkeypatch, capsys, tmp_path):
    import pre_tool_use as ptu

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    monkeypatch.setattr(
        ptu,
        "_enforce_breaker",
        lambda d: (None, "unifable breaker open: the flagged claim is grounded."),
    )
    payload = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "x.py")},
        "session_id": "edge",
        "cwd": str(tmp_path),
    }
    monkeypatch.setattr(ptu, "read_stdin_json", lambda: payload)
    rc = ptu.main()
    assert rc == 2  # evidence gate blocks: no spec exists
    err = capsys.readouterr().err
    assert "breaker open" in err.lower()


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
