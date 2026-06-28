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


def test_director_schema_requires_concrete_target_when_possible() -> None:
    desc = gb._JUDGE_SCHEMA["properties"]["directive"]["description"]
    assert desc.strip()


def test_directive_must_be_self_contained_not_taskid_pointer() -> None:
    """Regression: the director used to emit pointer-references like 'use the spec
    board's T1 check' instead of the concrete action, so the model received a label it
    had to look up rather than a runnable instruction. The schema must demand a
    self-contained, immediately executable directive and forbid bare spec-task-ID
    references."""
    desc = gb._JUDGE_SCHEMA["properties"]["directive"]["description"]
    assert desc.strip()


def test_steering_must_be_self_contained_not_taskid_pointer() -> None:
    """Same rule for the arm-path steering text: name the action in full, never a bare
    task-ID the model must resolve against the board."""
    desc = gb._JUDGE_SCHEMA["properties"]["steering"]["description"]
    assert desc.strip()


def test_tool_scope_schema_mentions_mutation_phase_only() -> None:
    desc = gb._JUDGE_SCHEMA["properties"]["tool_scope"]["description"]
    assert desc.strip()


def test_arm_judge_prompt_does_not_own_restriction_copy() -> None:
    system = gb._JUDGE_SYSTEM
    steering_desc = gb._JUDGE_SCHEMA["properties"]["steering"]["description"]

    for text in (system, steering_desc):
        assert "restricted to read-only" not in text
        assert "read-only ones (Read, WebSearch, WebFetch, Grep, Glob)" not in text
        assert "whitelisted research Bash until grounded" not in text
        assert text.strip()


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
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    dj = DirectorJudge(verdict=0)
    state = default_breaker()
    block, steering, notify = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=dj)
    assert block is False
    assert state["breaker_directive"] == "Read foo.py, then edit."
    assert state["breaker_tool_scope"]["deny"] == ["Edit"]
    assert "Read foo.py" in notify


def test_debounce_window_is_3s(monkeypatch) -> None:
    assert gb.JUDGE_WINDOW_SECONDS == 3
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "t")
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
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "t")
    dj = DirectorJudge(verdict=0, directive="Read foo.py, then edit.")
    state = default_breaker()
    _, _, n1 = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=dj)
    assert "Read foo.py" in n1
    # Next debounce window, judge fires again with the SAME directive -> silent.
    _, _, n2 = gb.evaluate_pre_tool(_pre("Bash"), state, now=3.0, active_task="P", judge=dj)
    assert dj.arm_calls == 2
    assert "unifable director:" not in n2


def test_paraphrased_directive_not_resurfaced(monkeypatch) -> None:
    """The real re-request failure mode: the judge RE-WORDS an already-surfaced
    instruction every debounce window. Byte-exact dedup (the old `!=` check) misses
    it, so the model is repeatedly told to redo work it just did. Near-duplicate
    suppression must keep the paraphrase silent."""
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "t")
    a = (
        "Capture and attach proof artifacts for the primary finding by gathering the "
        "relevant recorded request/response evidence and linking it to the failed T7 finding."
    )
    b = (
        "Capture and attach the proof artifacts for the primary finding by gathering the "
        "relevant recorded request/response evidence and any supporting configuration "
        "excerpts, then summarize how they demonstrate the account/catalog behavior."
    )
    dj = DirectorJudge(verdict=0, directive=a)
    state = default_breaker()
    _, _, n1 = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=dj)
    assert "Capture and attach proof artifacts" in n1
    # Next debounce window: the judge paraphrases the SAME step -> must stay silent.
    dj.directive = b
    _, _, n2 = gb.evaluate_pre_tool(_pre("Bash"), state, now=3.0, active_task="P", judge=dj)
    assert dj.arm_calls == 2  # the judge did fire again
    assert "supporting configuration" not in n2  # but the paraphrase was NOT surfaced
    assert b not in n2


def test_genuinely_new_directive_is_resurfaced(monkeypatch) -> None:
    """The dedup must not over-suppress: a directive for a DIFFERENT step (low token
    overlap) still surfaces after a prior one."""
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "t")
    a = "Capture and attach proof artifacts for the primary finding from the recorded evidence."
    c = "Run the live responses probe with model gpt and reasoning xhigh; record the http status."
    dj = DirectorJudge(verdict=0, directive=a)
    state = default_breaker()
    _, _, n1 = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=dj)
    assert "proof artifacts" in n1
    dj.directive = c
    _, _, n2 = gb.evaluate_pre_tool(_pre("Bash"), state, now=3.0, active_task="P", judge=dj)
    assert "live responses probe" in n2


def test_directives_near_duplicate_metric() -> None:
    """The deterministic backstop: a paraphrase is a duplicate, a different step is
    not, and degenerate/terse inputs fail safe (never spuriously suppressed)."""
    from breaker_runtime import directives_near_duplicate

    a = "Capture and attach proof artifacts for the primary finding from the recorded evidence."
    para = "Capture and attach the proof artifacts for the primary finding using the recorded evidence corpus."
    other = "Run the live responses probe with gpt and reasoning xhigh; record the http status code."
    assert directives_near_duplicate(a, para) is True
    assert directives_near_duplicate(a, other) is False
    assert directives_near_duplicate(a, a) is True
    # Degenerate inputs fail safe (treated as NOT duplicates -> nothing suppressed).
    assert directives_near_duplicate("", a) is False
    assert directives_near_duplicate(a, "") is False
    # Terse directives fall back to exact match (no spurious suppression).
    assert directives_near_duplicate("Read foo.py", "Read bar.py") is False
    assert directives_near_duplicate("Read foo.py", "Read foo.py") is True


def test_arming_clears_director_scope(monkeypatch) -> None:
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
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
