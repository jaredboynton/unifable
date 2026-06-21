#!/usr/bin/env python3
"""Logic tests for the overconfidence / groundedness breaker (groundedness.py).

Each test maps to a requirement of the breaker:
  R1  block ONLY Write/Edit/Bash; never WebSearch/Read/WebFetch/Grep/Glob
  R2  the SAME judge both arms (verdict 1) and disarms (verdict 0)
  R5  judge question = "did the model say something confidently w/o backing it up"
  R6  verdict 1 -> steering prompt returned + mutation blocked until evidence read
  R7  verdict 0 -> no steering, model sees nothing (no block)
  R8  judge on every tool; release grounded in activity (transcript)
  R9  debounced: <=1 judge call / 15s per session+user-prompt key
Run: python3 -m pytest tests/test_groundedness_breaker.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import groundedness as gb  # noqa: E402


# --- a recording judge: scripted verdicts + a call counter for debounce tests ---
class FakeJudge:
    def __init__(self, script):
        self.script = list(script)  # list of (verdict, steering)
        self.calls = 0
        self.systems = []

    def __call__(self, system, user, schema):
        self.systems.append(system)
        self.calls += 1
        verdict, steering = self.script[min(self.calls - 1, len(self.script) - 1)]
        return {"verdict": verdict, "steering": steering}


def _pre(tool, session="S", transcript="model: clearly the fix is X. (no evidence)"):
    # input_data shaped like a PreToolUse hook payload; transcript injected directly
    # by monkeypatching transcript_segment in tests that need a non-empty segment.
    return {"tool_name": tool, "session_id": session, "cwd": "/repo"}


# --------------------------------------------------------------------------- R1
def test_mutation_set_is_exactly_writes_edits_bash():
    for t in ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "Bash"):
        assert gb.is_mutation_tool(t), t
    for t in ("WebSearch", "Read", "WebFetch", "Grep", "Glob", "Task", "TodoWrite"):
        assert not gb.is_mutation_tool(t), t


def test_read_and_websearch_never_blocked_even_when_armed(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "model: definitely the cause is Y")
    judge = FakeJudge([(1, "you claimed Y with no proof; mutation blocked")])
    state = {}
    # arm via a mutation attempt first
    blocked, _ = gb.evaluate(_pre("Bash"), state, now=100.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    # now a Read within the debounce window: armed, but reads are never blocked
    blocked, steering = gb.evaluate(_pre("Read"), state, now=101.0, active_task="P", judge=judge)
    assert blocked is False and steering == ""
    # WebSearch likewise
    blocked, _ = gb.evaluate(_pre("WebSearch"), state, now=102.0, active_task="P", judge=judge)
    assert blocked is False


# --------------------------------------------------------------------------- R6
def test_verdict1_blocks_mutation_and_returns_steering(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "model: the fix is obviously Z")
    judge = FakeJudge([(1, "You asserted Z without reading the source. Write/Edit/Bash blocked until you cite evidence.")])
    state = {}
    blocked, steering = gb.evaluate(_pre("Edit"), state, now=10.0, active_task="P", judge=judge)
    assert blocked is True
    assert "blocked" in steering.lower() and steering != ""


# --------------------------------------------------------------------------- R7
def test_verdict0_no_block_no_steering(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "model: I read foo.py:10, it does X; here is the edit")
    judge = FakeJudge([(0, "")])
    state = {}
    blocked, steering = gb.evaluate(_pre("Write"), state, now=10.0, active_task="P", judge=judge)
    assert blocked is False and steering == ""
    assert state["breaker_armed"] is False


# --------------------------------------------------------------------------- R2/R8
def test_same_judge_arms_then_disarms(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = FakeJudge([(1, "unproven; blocked"), (0, "")])  # 1st call arms, 2nd disarms
    state = {}
    blocked, _ = gb.evaluate(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    # past the 15s window -> judge re-runs, sees grounding, disarms
    blocked, steering = gb.evaluate(_pre("Bash"), state, now=20.0, active_task="P", judge=judge)
    assert blocked is False and steering == "" and state["breaker_armed"] is False
    assert judge.calls == 2


# --------------------------------------------------------------------------- R9
def test_debounce_at_most_one_judge_call_per_15s_per_key(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")
    judge = FakeJudge([(1, "blocked")])
    state = {}
    gb.evaluate(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    gb.evaluate(_pre("Bash"), state, now=5.0, active_task="P", judge=judge)   # within window
    gb.evaluate(_pre("Bash"), state, now=14.9, active_task="P", judge=judge)  # within window
    assert judge.calls == 1
    gb.evaluate(_pre("Bash"), state, now=15.0, active_task="P", judge=judge)  # window elapsed
    assert judge.calls == 2


def test_debounce_key_is_session_plus_user_prompt(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")
    judge = FakeJudge([(1, "blocked")])
    state = {}
    gb.evaluate(_pre("Bash", session="S"), state, now=0.0, active_task="P1", judge=judge)
    # same session, NEW user prompt -> different key -> re-judge even within 15s
    gb.evaluate(_pre("Bash", session="S"), state, now=1.0, active_task="P2", judge=judge)
    assert judge.calls == 2
    # new session, same prompt -> different key -> re-judge
    gb.evaluate(_pre("Bash", session="S2"), state, now=1.5, active_task="P2", judge=judge)
    assert judge.calls == 3


# --------------------------------------------------------------------------- fail-open
def test_judge_exception_fails_open(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")

    def boom(system, user, schema):
        raise RuntimeError("realtime down")

    state = {}
    blocked, steering = gb.evaluate(_pre("Bash"), state, now=0.0, active_task="P", judge=boom)
    assert blocked is False and steering == ""


def test_disabled_env_fails_open(monkeypatch):
    monkeypatch.setenv("UNIFABLE_BREAKER", "0")
    judge = FakeJudge([(1, "blocked")])
    state = {}
    blocked, _ = gb.evaluate(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    assert blocked is False and judge.calls == 0


# --------------------------------------------------------------------------- R5 (prompt) / judge plumbing
def test_judge_system_prompt_asks_the_confidence_question():
    seg = "model said something"
    judge = FakeJudge([(1, "x")])
    gb.judge_segment(seg, judge=judge)
    sysp = judge.systems[0].lower()
    assert "confidently without backing it up" in sysp
    assert "write/edit/bash" in sysp


def test_empty_segment_is_not_a_violation():
    judge = FakeJudge([(1, "should not be used")])
    verdict, steering = gb.judge_segment("", judge=judge)
    assert verdict == 0 and steering == "" and judge.calls == 0


def test_verdict0_forces_empty_steering_even_if_model_returns_text():
    # defense-in-depth: a verdict-0 with stray steering text must not leak a prompt
    judge = FakeJudge([(0, "stray text that should be dropped")])
    verdict, steering = gb.judge_segment("seg", judge=judge)
    assert verdict == 0 and steering == ""


# --------------------------------------------------------------------------- transcript
def test_transcript_segment_reads_path(tmp_path):
    import json
    f = tmp_path / "t.jsonl"
    f.write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "the fix is X"}]}}) + "\n"
        + json.dumps({"type": "user", "message": {"role": "user",
                      "content": [{"type": "tool_result", "content": "ran probe -> 0 ok"}]}}) + "\n",
        encoding="utf-8",
    )
    seg = gb.transcript_segment({"transcript_path": str(f)})
    assert "the fix is X" in seg and "ran probe" in seg


def test_transcript_segment_missing_is_empty():
    assert gb.transcript_segment({"transcript_path": "/no/such.jsonl", "session_id": "z", "cwd": "/x"}) == ""


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
