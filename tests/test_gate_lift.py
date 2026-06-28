#!/usr/bin/env python3
"""Judge-granted evidence-gate lift (the director in control of the gate).

A trivial, explicitly-requested mutation (e.g. `cp a b`) is blocked by the
research-phase evidence gate when the task's profile can never be satisfied by
that action. The director judge may LIFT that specific block synchronously on the
blocking PreToolUse call, so the main model's first attempt succeeds. The lift is
scoped, budgeted, fail-closed, and subordinate to the absolute guards
(PROTECTED_PATHS, dangerous-env) which are enforced upstream and never lifted.

Run: python3 -m pytest tests/test_gate_lift.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import gate_lift as gl  # noqa: E402
import pre_tool_use as ptu  # noqa: E402

# --- pure predicate / budget ------------------------------------------------


def test_action_signature_distinguishes_actions():
    assert gl.action_signature("Bash", "cp a b", None) == gl.action_signature("Bash", "cp a b", None)
    assert gl.action_signature("Bash", "cp a b", None) != gl.action_signature("Bash", "rm a", None)
    # write actions sign by sorted target set, order-independent
    assert gl.action_signature("Write", None, ["b", "a"]) == gl.action_signature("Write", None, ["a", "b"])


def test_lift_allows_exact_match_only_and_budget():
    state: dict = {}
    gl.record_lift(state, "Bash", "cp a b", None, "copy a to b")
    lift = state["breaker_gate_lift"]
    assert gl.lift_allows(lift, "Bash", "cp a b", None) is True
    # a different command is NOT covered -> caller must re-judge it
    assert gl.lift_allows(lift, "Bash", "rm a", None) is False
    # budget exhaustion blocks reuse
    lift["uses"] = 0
    assert gl.lift_allows(lift, "Bash", "cp a b", None) is False
    # malformed lift is fail-safe
    assert gl.lift_allows({}, "Bash", "cp a b", None) is False
    assert gl.lift_allows(None, "Bash", "cp a b", None) is False


def test_record_and_consume_budget():
    state: dict = {}
    gl.record_lift(state, "Bash", "cp a b", None, "copy")
    assert state["breaker_gate_lift"]["uses"] == gl.lift_uses_budget()
    gl.consume_lift(state)
    assert state["breaker_gate_lift"]["uses"] == gl.lift_uses_budget() - 1


def test_judge_call_cap(monkeypatch):
    monkeypatch.setenv("UNIFABLE_GATE_LIFT_MAX_JUDGE", "2")
    state = {"breaker_gate_lift_calls": 0}
    assert gl.judge_budget_left(state) is True
    gl.bump_judge_calls(state)
    gl.bump_judge_calls(state)
    assert state["breaker_gate_lift_calls"] == 2
    assert gl.judge_budget_left(state) is False


def test_judge_gate_lift_fail_closed_on_judge_error(monkeypatch):
    import codex_judge
    import judge_transport

    def boom(*a, **k):
        raise codex_judge.JudgeError("down")

    monkeypatch.setattr(judge_transport, "ask_structured", boom)
    out = gl.judge_gate_lift(goal="copy a to b", tool_name="Bash", command="cp a b", paths=["b"])
    assert out["lift"] == 0


# --- _enforce_bash integration ----------------------------------------------


def _bash_input(tmp_path, command):
    return (
        {"session_id": "lift-test", "cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": command}},
        {"command": command},
    )


def _prep(monkeypatch, tmp_path):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_GRADE", raising=False)  # default STANDARD -> gate active


def test_blocked_cp_lifted_when_judge_allows(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    monkeypatch.setattr(gl, "judge_gate_lift", lambda **k: {"lift": 1, "scope": "copy", "paths": ["b"]})
    input_data, tool_input = _bash_input(tmp_path, "cp /src/a /dst/b")
    rc = ptu._enforce_bash(input_data, tool_input, str(tmp_path), tool_name="Bash")
    assert rc == 0  # lifted -> allowed


def test_blocked_cp_stays_blocked_when_judge_denies(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    monkeypatch.setattr(gl, "judge_gate_lift", lambda **k: {"lift": 0, "scope": "", "paths": []})
    input_data, tool_input = _bash_input(tmp_path, "cp /src/a /dst/b")
    rc = ptu._enforce_bash(input_data, tool_input, str(tmp_path), tool_name="Bash")
    assert rc != 0  # denied -> still blocked


def test_lift_budget_reuses_grant_without_second_judge_call(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    calls = {"n": 0}

    def counting_judge(**k):
        calls["n"] += 1
        return {"lift": 1, "scope": "copy", "paths": ["b"]}

    monkeypatch.setattr(gl, "judge_gate_lift", counting_judge)
    input_data, tool_input = _bash_input(tmp_path, "cp /src/a /dst/b")
    assert ptu._enforce_bash(input_data, tool_input, str(tmp_path), tool_name="Bash") == 0
    assert ptu._enforce_bash(input_data, tool_input, str(tmp_path), tool_name="Bash") == 0
    assert calls["n"] == 1  # second identical action reused the grant, no re-judge


def test_non_mutating_read_bash_is_not_lifted(monkeypatch, tmp_path):
    # A non-whitelisted READ keeps its deterministic research block and never
    # triggers a lift judge call -- the agent should use Read/whitelisted tools.
    _prep(monkeypatch, tmp_path)
    called = {"n": 0}
    monkeypatch.setattr(gl, "judge_gate_lift", lambda **k: called.__setitem__("n", called["n"] + 1) or {"lift": 1})
    for read_cmd in ("cat", "nl", "pwd"):
        input_data, tool_input = _bash_input(tmp_path, read_cmd)
        rc = ptu._enforce_bash(input_data, tool_input, str(tmp_path), tool_name="Bash")
        assert rc != 0, read_cmd  # stays blocked
    assert called["n"] == 0  # lift judge never consulted for reads


def test_whitelisted_research_bash_never_calls_judge(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    called = {"n": 0}
    monkeypatch.setattr(gl, "judge_gate_lift", lambda **k: called.__setitem__("n", called["n"] + 1) or {"lift": 1})
    for command in ("ls -la /dst", "cat README.md", "nl -ba README.md"):
        input_data, tool_input = _bash_input(tmp_path, command)
        assert ptu._enforce_bash(input_data, tool_input, str(tmp_path), tool_name="Bash") == 0
    assert called["n"] == 0  # ls is allowed research -> the lift path is never reached


# --- absolute guards stay absolute (main() pipeline) ------------------------


def test_protected_write_is_never_lifted(monkeypatch, tmp_path):
    _prep(monkeypatch, tmp_path)
    judged = {"n": 0}
    monkeypatch.setattr(gl, "judge_gate_lift", lambda **k: judged.__setitem__("n", judged["n"] + 1) or {"lift": 1})
    monkeypatch.setattr(ptu, "_enforce_breaker", lambda input_data: (None, ""))
    protected = str(tmp_path / ".unifable" / "specs" / "x" / "spec.json")
    input_data = {
        "session_id": "lift-test",
        "cwd": str(tmp_path),
        "tool_name": "Write",
        "tool_input": {"file_path": protected, "content": "x"},
    }
    monkeypatch.setattr(ptu, "read_stdin_json", lambda: input_data)
    rc = ptu.main()
    assert rc != 0  # protected path wins over any lift
    assert judged["n"] == 0  # the lift judge is never even consulted for protected paths


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
