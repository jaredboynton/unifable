"""Advisory judge hints: a non-blocking nudge the judge can emit at any point.

The load-bearing invariant: a hint NEVER changes a verdict, NEVER changes a task
status, and NEVER opens/lifts the completion breaker. It is advisory context
only. These tests lock that invariant and cover the three surfaces:
  - the hint field on judge_task / judge_dispute (rides the existing judge call),
  - the verdict-free judge_hint() used by the proactive loops,
  - the Stop completion-breaker loop (gate_stop) and the PostToolUse
    repeated-failure loop (gate_post_tool).

The network judge is stubbed (spec.judge_task / spec.judge_dispute /
spec.judge_hint, or codex_judge.ask_structured), matching test_spec_lifecycle.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    _cmd_validate_task,
    _normalize_hint,
    all_tasks_validated,
    judge_hint,
    load_spec,
    save_spec,
    spec_template,
)


def _task(tid, status):
    return {"id": tid, "title": tid, "check": "true", "status": status}


def _seed(tmp_path, status="delivered"):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "do x"
    s["tasks"] = [_task("T1", status)]
    save_spec(str(tmp_path), "K", s)
    return SimpleNamespace(root=str(tmp_path), task_id="K", task="T1")


# --- _normalize_hint --------------------------------------------------------

def test_normalize_hint_drops_empty_and_placeholders():
    assert _normalize_hint("") == ""
    assert _normalize_hint("   ") == ""
    assert _normalize_hint(None) == ""
    for ph in ("tbd", "N/A", "none", "no hint", "Nothing", "unsure"):
        assert _normalize_hint(ph) == "", ph


def test_normalize_hint_collapses_and_caps():
    assert _normalize_hint("  do   this\n  now ") == "do this now"
    long = _normalize_hint("x" * 400)
    assert len(long) <= 280 and long.endswith("...")


# --- hint rides the judge verdict (validate-task / dispute) -----------------

def test_validate_task_pass_stores_and_surfaces_hint(tmp_path, monkeypatch):
    args = _seed(tmp_path)
    monkeypatch.setattr(spec_mod, "judge_task",
                        lambda sp, t, ec, out: (1, "ok", [], "consider adding an error-path test"))
    assert _cmd_validate_task(args) == 0
    t = load_spec(str(tmp_path), "K")["tasks"][0]
    assert t["status"] == "validated"
    assert t["judge_hint"] == "consider adding an error-path test"


def test_dispute_reject_carries_hint(tmp_path, monkeypatch):
    args = _seed(tmp_path, status="disputed")
    s = load_spec(str(tmp_path), "K"); s["tasks"][0]["dispute_evidence"] = "too hard"
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(spec_mod, "judge_dispute",
                        lambda sp, t, e: (0, "not impossible", "try mocking the upstream call"))
    assert _cmd_validate_task(args) == 2
    t = load_spec(str(tmp_path), "K")["tasks"][0]
    assert t["status"] == "failed"
    assert t["judge_hint"] == "try mocking the upstream call"


# --- PROTECTED INVARIANT: a hint never resolves a task or opens the breaker --

def test_hint_with_failing_verdict_keeps_task_failed_and_breaker_closed(tmp_path, monkeypatch):
    args = _seed(tmp_path)
    monkeypatch.setattr(spec_mod, "judge_task",
                        lambda sp, t, ec, out: (0, "no real evidence", [], "go run the actual suite"))
    rc = _cmd_validate_task(args)
    assert rc == 2  # still a failure exit
    spec = load_spec(str(tmp_path), "K")
    t = spec["tasks"][0]
    assert t["status"] == "failed"                       # hint did not resolve it
    assert t["judge_hint"] == "go run the actual suite"  # hint recorded
    ok, incomplete = all_tasks_validated(spec)
    assert not ok and incomplete == ["T1"]               # breaker stays CLOSED


def test_judge_hint_is_failopen_and_never_mutates_spec(monkeypatch):
    import codex_judge

    spec = {"restated_goal": "g", "tasks": [_task("T1", "failed")]}
    before = copy.deepcopy(spec)

    def boom(*a, **k):
        raise codex_judge.JudgeError("judge down")

    monkeypatch.setattr(codex_judge, "ask_structured", boom)
    assert judge_hint(spec, signal="looping", recent="x") == ""  # silent, no raise
    assert spec == before                                        # mutated nothing


def test_judge_hint_returns_clean_string_and_drops_placeholder(monkeypatch):
    import codex_judge

    spec = {"restated_goal": "g", "tasks": []}
    monkeypatch.setattr(codex_judge, "ask_structured", lambda *a, **k: {"hint": "  do  X "})
    assert judge_hint(spec, signal="s") == "do X"
    monkeypatch.setattr(codex_judge, "ask_structured", lambda *a, **k: {"hint": "TBD"})
    assert judge_hint(spec, signal="s") == ""


# --- Stop completion-breaker loop (gate_stop) -------------------------------

def _run_stop(gate_stop, payload):
    captured = {"out": {}}
    gate_stop.read_stdin_json = lambda: payload
    gate_stop.emit_json = lambda d: captured.__setitem__("out", d)
    gate_stop.main()
    return captured["out"]


def test_stop_loop_appends_hint_at_threshold_without_lifting_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    import gate_stop

    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "ship the thing"
    spec["tasks"] = [_task("T1", "failed")]
    save_spec(str(tmp_path), "hintsess", spec)

    payload = {"session_id": "hintsess", "cwd": str(tmp_path)}
    monkeypatch.setattr(spec_mod, "judge_hint",
                        lambda sp, *, signal, recent="": "stop re-running validate-task; fix the check first")

    outs = [_run_stop(gate_stop, payload) for _ in range(3)]
    # every attempt is blocked -- the gate holds regardless of the hint
    assert all(o.get("decision") == "block" for o in outs)
    assert all("breaker CLOSED" in o.get("reason", "") for o in outs)
    # the nudge appears only once the agent has plausibly looped (3rd block)
    assert "Hint (advisory, does not lift the gate):" not in outs[0].get("reason", "")
    assert "Hint (advisory, does not lift the gate): stop re-running" in outs[2].get("reason", "")
    # the task was never resolved and the breaker never opened
    final = load_spec(str(tmp_path), "hintsess")
    assert final["tasks"][0]["status"] == "failed"
    assert all_tasks_validated(final)[0] is False


def test_stop_loop_failopen_when_judge_raises_still_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    import gate_stop

    spec = spec_template(); spec["requires_tasks"] = True
    spec["restated_goal"] = "g"; spec["tasks"] = [_task("T1", "failed")]
    save_spec(str(tmp_path), "boomsess", spec)
    payload = {"session_id": "boomsess", "cwd": str(tmp_path)}

    def boom(*a, **k):
        raise RuntimeError("judge exploded")

    monkeypatch.setattr(spec_mod, "judge_hint", boom)
    outs = [_run_stop(gate_stop, payload) for _ in range(3)]
    assert all(o.get("decision") == "block" for o in outs)        # gate never lifts
    assert "Hint (advisory" not in outs[2].get("reason", "")       # no hint, no crash


# --- PostToolUse repeated-failure loop (gate_post_tool) ---------------------

def _run_post(gate_post_tool, payload):
    captured = {"out": {}}
    gate_post_tool.read_stdin_json = lambda: payload
    gate_post_tool.emit_json = lambda d: captured.__setitem__("out", d)
    gate_post_tool.main()
    return (captured["out"].get("hookSpecificOutput") or {}).get("additionalContext") or ""


def _seed_failure(payload, summary):
    from ledger import load_ledger, save_ledger

    led = load_ledger(payload)
    led["failures"] = [{"kind": "tool-result", "summary": summary}]
    save_ledger(payload, led)


def test_post_tool_repeated_failure_emits_judge_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    import gate_post_tool

    payload = {
        "session_id": "rep", "cwd": str(tmp_path), "tool_name": "Bash",
        "tool_input": {"command": "pytest"},
        "tool_response": {"exit_code": 1, "stdout": "boom error here"},
    }
    _seed_failure(payload, "boom error here")
    monkeypatch.setattr(spec_mod, "judge_hint",
                        lambda sp, *, signal, recent="": "change approach: the import path is wrong")
    ctx = _run_post(gate_post_tool, payload)
    assert "Hint (advisory, not a gate): change approach" in ctx
    # the removed deterministic nag must not reappear
    assert "Stop retrying silently" not in ctx


def test_post_tool_repeated_failure_failopen_when_judge_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    import gate_post_tool

    payload = {
        "session_id": "rep2", "cwd": str(tmp_path), "tool_name": "Bash",
        "tool_input": {"command": "pytest"},
        "tool_response": {"exit_code": 1, "stdout": "boom error here"},
    }
    _seed_failure(payload, "boom error here")

    def boom(*a, **k):
        raise RuntimeError("judge exploded")

    monkeypatch.setattr(spec_mod, "judge_hint", boom)
    ctx = _run_post(gate_post_tool, payload)  # must not raise
    assert "Hint (advisory" not in ctx
