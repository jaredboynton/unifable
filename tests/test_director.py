#!/usr/bin/env python3
"""Tests for the stepwise DIRECTOR on the per-tool judge (groundedness.py).

The per-tool judge now also emits, on every debounced call, a minimal next-step
directive and a tool_scope. These ride the SAME single judge call as the
overconfidence arm verdict (no second round-trip) and are persisted to breaker
state so the deterministic tool_scope predicate can enforce them between calls.

Requirements:
  D1  arm_judge still returns its 3-tuple unchanged; an optional `out` dict
      captures directive + tool_scope from the same judge object.
  D2  the directive is token-bounded (truncated).
  D3  evaluate_pre_tool persists breaker_directive + breaker_tool_scope and
      surfaces the directive on the allow path (~once per debounce window).
  D4  the judge debounce window is 3s (down from 15s).
  D5  when the breaker ARMS, the director scope is cleared (the breaker owns the
      block while armed).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import groundedness as gb  # noqa: E402
from breaker_state import default_breaker  # noqa: E402


def _pre(tool, session="S"):
    return {"tool_name": tool, "session_id": session, "cwd": "/repo"}


class DirectorJudge:
    """Arm-path judge that also returns a directive + tool_scope."""

    def __init__(self, *, verdict=0, directive="Read foo.py, then edit.", scope=None, grounded=1):
        self.verdict = verdict
        self.directive = directive
        self.scope = scope if scope is not None else {"allow": ["Read", "Grep"], "deny": ["Edit"]}
        self.grounded = grounded
        self.arm_calls = 0
        self.disarm_calls = 0

    def __call__(self, system, user, schema):
        if "release monitor" in system.lower():
            self.disarm_calls += 1
            return {
                "grounded": self.grounded,
                "needed": "" if self.grounded else "read foo.py",
                "load_bearing": 1,
                "provisional_release": 0,
                "lift_reason": "",
                "lift_scope": "",
            }
        self.arm_calls += 1
        return {
            "verdict": self.verdict,
            "steering": "blocked" if self.verdict == 1 else "",
            "claim": "the cause is Y" if self.verdict == 1 else "",
            "load_bearing": 1 if self.verdict == 1 else 0,
            "directive": self.directive,
            "tool_scope": self.scope,
        }


def test_arm_judge_captures_director_fields_via_out() -> None:
    dj = DirectorJudge(verdict=0)
    out: dict = {}
    verdict, steering, claim = gb.arm_judge("a non-empty segment", judge=dj, out=out)
    # 3-tuple contract unchanged.
    assert (verdict, steering, claim) == (0, "", "")
    # Director fields captured from the SAME judge object.
    assert out["directive"] == "Read foo.py, then edit."
    assert out["tool_scope"]["deny"] == ["Edit"]
    # Directive is folded into the scope so the scope predicate can surface it.
    assert out["tool_scope"]["directive"] == "Read foo.py, then edit."


def test_arm_judge_without_out_is_backward_compatible() -> None:
    dj = DirectorJudge(verdict=0)
    result = gb.arm_judge("a non-empty segment", judge=dj)
    assert result == (0, "", "")


def test_directive_is_truncated() -> None:
    long = "x" * 5000
    dj = DirectorJudge(verdict=0, directive=long)
    out: dict = {}
    gb.arm_judge("seg", judge=dj, out=out)
    assert len(out["directive"]) <= gb.DIRECTIVE_MAX_CHARS


def test_evaluate_persists_and_surfaces_directive(monkeypatch) -> None:
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    dj = DirectorJudge(verdict=0)
    state = default_breaker()
    block, steering, notify = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=dj)
    assert block is False
    assert state["breaker_directive"] == "Read foo.py, then edit."
    assert state["breaker_tool_scope"]["deny"] == ["Edit"]
    assert "Read foo.py" in notify


def test_debounce_window_is_3s(monkeypatch) -> None:
    assert gb.JUDGE_WINDOW_SECONDS == 3
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")
    dj = DirectorJudge(verdict=0)
    state = default_breaker()
    gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=dj)
    gb.evaluate_pre_tool(_pre("Bash"), state, now=1.0, active_task="P", judge=dj)
    gb.evaluate_pre_tool(_pre("Bash"), state, now=2.9, active_task="P", judge=dj)
    assert dj.arm_calls == 1
    gb.evaluate_pre_tool(_pre("Bash"), state, now=3.0, active_task="P", judge=dj)
    assert dj.arm_calls == 2


def test_unchanged_directive_not_resurfaced(monkeypatch) -> None:
    """Token-aware: an identical directive is surfaced once, not every window."""
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")
    dj = DirectorJudge(verdict=0, directive="Read foo.py, then edit.")
    state = default_breaker()
    _, _, n1 = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=dj)
    assert "Read foo.py" in n1
    # Next debounce window, judge fires again with the SAME directive -> silent.
    _, _, n2 = gb.evaluate_pre_tool(_pre("Bash"), state, now=3.0, active_task="P", judge=dj)
    assert dj.arm_calls == 2
    assert "unifable director:" not in n2


def test_arming_clears_director_scope(monkeypatch) -> None:
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    dj = DirectorJudge(verdict=1)
    state = default_breaker()
    block, steering, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=0.0, active_task="P", judge=dj)
    assert block is True
    assert state["breaker_armed"] is True
    # While armed the breaker owns the block; the director scope must not also fire.
    assert state["breaker_tool_scope"] == {}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
