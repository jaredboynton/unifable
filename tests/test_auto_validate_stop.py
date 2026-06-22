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
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_task", lambda sp, t, ec, out: (1, "ok", [], ""))
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
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_task", lambda sp, t, ec, out: (1, "ok", [], ""))

    captured = {"out": {}}
    gate_stop.read_stdin_json = lambda: {"session_id": "sess", "cwd": str(tmp_path)}
    gate_stop.emit_json = lambda d: captured.__setitem__("out", d)
    gate_stop.main()
    assert captured["out"] == {}  # not blocked
    assert load_spec(str(tmp_path), "sess")["tasks"][0]["status"] == "validated"
