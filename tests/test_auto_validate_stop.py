#!/usr/bin/env python3
"""auto_validate_spec: harness runs checks+judge on stop (no validate-task CLI)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    all_tasks_validated,
    auto_validate_spec,
    load_spec,
    save_spec,
    spec_template,
    _cmd_dispute,
)


def _task(tid, status):
    return {"id": tid, "title": tid, "check": "true", "status": status}


def test_auto_validate_passes_pending_task(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items: [(1, "ok", [], "", "") for _ in items])
    spec, msgs = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert spec["tasks"][0]["status"] == "validated"
    assert all_tasks_validated(spec)[0] is True
    assert msgs


def test_auto_validate_adjudicates_dispute(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["tasks"] = [_task("T1", "failed")]
    save_spec(str(tmp_path), "K", s)
    args = SimpleNamespace(root=str(tmp_path), task_id="K", task="T1", evidence="impossible")
    _cmd_dispute(args)
    monkeypatch.setattr(spec_mod, "judge_dispute", lambda sp, t, e: (1, "accepted", ""))
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert spec["tasks"][0]["status"] == "retracted"


def _run_stop(gate_stop, payload):
    captured = {"out": {}}
    gate_stop.read_stdin_json = lambda: payload
    gate_stop.emit_json = lambda d: captured.__setitem__("out", d)
    gate_stop.main()
    return captured["out"]


def test_stop_runs_auto_validate_before_breaker_check(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items: [(1, "ok", [], "", "") for _ in items])

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") != "block"
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "stop validation" in ctx
    assert load_spec(str(tmp_path), "sess")["tasks"][0]["status"] == "validated"


def test_stop_forwards_dispute_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "failed")]
    save_spec(str(tmp_path), "sess", s)
    args = SimpleNamespace(root=str(tmp_path), task_id="sess", task="T1", evidence="not possible")
    _cmd_dispute(args)
    reason = "Rejected. The evidence does not prove impossibility."
    monkeypatch.setattr(spec_mod, "judge_dispute", lambda sp, t, e: (0, reason, ""))

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    block_reason = out.get("reason") or ""
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    # reason carries only the short alarm; the board (judge reason + dispute
    # headline) rides additionalContext and is no longer duplicated into reason.
    assert "breaker CLOSED" in block_reason
    assert reason in ctx
    assert reason not in block_reason
    assert "T1: dispute rejected" in ctx


def test_stop_board_not_duplicated_into_reason(tmp_path, monkeypatch):
    """The spec board rides additionalContext only; reason keeps just the alarm."""
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(
        spec_mod, "judge_tasks", lambda sp, items: [(0, "T1 needs more proof", [], "", "") for _ in items]
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    block_reason = out.get("reason") or ""
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "breaker CLOSED" in block_reason
    assert "T1 needs more proof" in ctx           # judge detail in additionalContext
    assert "T1 needs more proof" not in block_reason  # not duplicated into reason
    assert "unifable spec update" not in block_reason  # board not in reason at all


def test_stop_forwards_three_task_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending"), _task("T2", "pending"), _task("T3", "pending")]
    save_spec(str(tmp_path), "sess", s)

    def fake_judge_tasks(sp, items):
        out = []
        for it in items:
            tid = it["task"]["id"]
            out.append((0, f"{tid} lacks evidence", [], "", ""))
        return out

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(spec_mod, "judge_tasks", fake_judge_tasks)

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    for tid in ("T1", "T2", "T3"):
        assert f"{tid} lacks evidence" in ctx


def test_stop_validate_context_builder_failopen_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items: [(0, "no", [], "", "") for _ in items])
    monkeypatch.setattr(
        gate_stop,
        "_build_stop_validate_context",
        lambda spec, val_msgs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    assert "breaker CLOSED" in (out.get("reason") or "")
