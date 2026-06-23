"""Append-only spec lifecycle: the agent adds requirements, only the judge removes."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    all_tasks_validated,
    auto_validate_spec,
    load_spec,
    save_spec,
    spec_template,
    validate_spec,
    _cmd_add_task,
    _cmd_dispute,
    _cmd_restate,
)


def _task(tid, status):
    return {"id": tid, "title": tid, "check": "true", "status": status}


def test_requires_tasks_empty_spec_blocks_completion():
    s = spec_template(); s["requires_tasks"] = True; s["tasks"] = []
    ok, incomplete = all_tasks_validated(s)
    assert not ok and incomplete


def test_retracted_counts_as_resolved():
    s = {"requires_tasks": True, "tasks": [_task("T1", "validated"), _task("T2", "retracted")]}
    assert all_tasks_validated(s) == (True, [])


def test_disputed_does_not_resolve():
    s = {"requires_tasks": True, "tasks": [_task("T1", "validated"), _task("T2", "disputed")]}
    ok, incomplete = all_tasks_validated(s)
    assert not ok and incomplete == ["T2"]


def _seed(tmp_path, status="failed"):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "do x"
    s["tasks"] = [_task("T1", status)]
    save_spec(str(tmp_path), "K", s)
    return SimpleNamespace(root=str(tmp_path), task_id="K", task="T1")


def test_dispute_sets_status_and_stores_evidence(tmp_path):
    args = _seed(tmp_path)
    args.evidence = "the upstream API has no such endpoint; 404 on every variant"
    rc = _cmd_dispute(args)
    assert rc == 0
    t = load_spec(str(tmp_path), "K")["tasks"][0]
    assert t["status"] == "disputed"
    assert "404" in t["dispute_evidence"]


def test_auto_validate_accepts_dispute_and_retracts(tmp_path, monkeypatch):
    args = _seed(tmp_path)
    args.evidence = "proven impossible"
    _cmd_dispute(args)
    monkeypatch.setattr(spec_mod, "judge_dispute", lambda s, t, e: (1, "genuinely impossible"))
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert spec["tasks"][0]["status"] == "retracted"


def test_auto_validate_appends_judge_requirements(tmp_path, monkeypatch):
    s = spec_template(); s["requires_tasks"] = True; s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(
        spec_mod, "judge_task",
        lambda sp, t, ec, out: (1, "ok", [{"title": "also handle errors", "check": "true"}], ""),
    )
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    tasks = spec["tasks"]
    assert [t["id"] for t in tasks] == ["T1", "T2"]
    assert tasks[0]["status"] == "validated"
    assert tasks[1]["status"] == "pending" and tasks[1]["added_by"] == "judge"


def test_scaffold_hook_creates_requires_tasks_spec(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    import gate_prompt
    path = gate_prompt._ensure_spec_scaffold(str(tmp_path), "K", "Refactor the auth module please")
    assert path and Path(path).exists()
    s = load_spec(str(tmp_path), "K")
    assert s["requires_tasks"] is True
    assert s["tasks"] == []
    assert "auth module" in s["restated_goal"]
    assert all_tasks_validated(s)[0] is False


def test_seeded_goal_blocks_until_restated(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    import gate_prompt
    from spec import _cmd_add_task, _cmd_restate, validate_spec
    gate_prompt._ensure_spec_scaffold(str(tmp_path), "K", "make the parser faster")
    _cmd_add_task(SimpleNamespace(root=str(tmp_path), task_id="K", title="t", check="true"))
    s = load_spec(str(tmp_path), "K")
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://x", "why": "fetched this session"}]
    save_spec(str(tmp_path), "K", s)
    ok, reasons = validate_spec(load_spec(str(tmp_path), "K"), "STANDARD", require_evidence=True)
    assert not ok and any("restate" in r.lower() for r in reasons), reasons
    assert _cmd_restate(SimpleNamespace(
        root=str(tmp_path), task_id="K", goal="Cut parser latency by streaming tokens",
    )) == 0
    s = load_spec(str(tmp_path), "K")
    assert s["goal_seeded"] is False and "latency" in s["restated_goal"]
    ok2, reasons2 = validate_spec(s, "STANDARD", require_evidence=True)
    assert ok2, reasons2


def test_end_to_end_add_task_and_auto_validate(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    import gate_prompt
    gate_prompt._ensure_spec_scaffold(str(tmp_path), "K", "do the thing")
    from spec import _cmd_restate
    _cmd_restate(SimpleNamespace(root=str(tmp_path), task_id="K", goal="Make greet reject empty names"))
    _cmd_add_task(SimpleNamespace(root=str(tmp_path), task_id="K", title="thing works", check="true"))
    s = load_spec(str(tmp_path), "K")
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://x", "why": "fetched this session"}]
    save_spec(str(tmp_path), "K", s)
    ok, reasons = validate_spec(s, "STANDARD", require_evidence=True)
    assert ok, reasons
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_task", lambda sp, t, ec, out: (1, "ok", [], ""))
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert all_tasks_validated(spec) == (True, [])
