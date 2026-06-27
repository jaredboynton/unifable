#!/usr/bin/env python3
"""Judge-owned task-board reconciliation from captured evidence."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(GATE))
sys.path.insert(0, str(HOOKS))

from spec import (  # noqa: E402
    _apply_reconcile_actions,
    all_tasks_validated,
    judge_reconcile_spec,
    load_spec,
    save_spec,
    spec_template,
)


def _task(tid: str, title: str = "req", status: str = "failed") -> dict:
    return {"id": tid, "title": title, "check": "rg old-route src", "status": status, "added_by": "agent"}


def test_reconcile_retracts_obsolete_task_with_captured_evidence():
    spec = {"requires_tasks": True, "tasks": [_task("T1", "Old route still exists")]}
    evidence = {"command_outputs": ["rg old-route src -> 0 matches; old route was removed"]}
    actions = [
        {
            "action": "retract",
            "id": "T1",
            "reason": "old route is gone",
            "evidence_refs": ["rg old-route src -> 0 matches"],
        }
    ]
    headlines = _apply_reconcile_actions(spec, actions, evidence=evidence)
    assert spec["tasks"][0]["status"] == "retracted"
    assert spec["tasks"][0]["judge_reason"] == "old route is gone"
    assert spec["tasks"][0]["reconcile_evidence_refs"] == ["rg old-route src -> 0 matches"]
    assert all_tasks_validated(spec)[0] is True
    assert headlines == ["Judge retracted T1: old route is gone"]


def test_reconcile_ignores_lifecycle_action_without_captured_evidence():
    spec = {"requires_tasks": True, "tasks": [_task("T1", "Hard thing", "failed")]}
    actions = [
        {
            "action": "retract",
            "id": "T1",
            "reason": "too hard",
            "evidence_refs": ["not in ledger"],
        }
    ]
    headlines = _apply_reconcile_actions(spec, actions, evidence={"command_outputs": ["pytest -> failed"]})
    assert spec["tasks"][0]["status"] == "failed"
    assert headlines == []


def test_reconcile_supersedes_old_task_with_replacement():
    spec = {"requires_tasks": True, "tasks": [_task("T1", "Old route still exists", "failed")]}
    evidence = {"command_outputs": ["rg old-route src -> 0 matches; new-route exists in src/app.py"]}
    actions = [
        {
            "action": "supersede",
            "title": "New route is covered",
            "check": "rg new-route src/app.py",
            "reason": "implementation pivoted to the new route",
            "evidence_refs": ["new-route exists"],
            "supersedes": ["T1"],
        }
    ]
    headlines = _apply_reconcile_actions(spec, actions, evidence=evidence)
    by_id = {t["id"]: t for t in spec["tasks"]}
    assert by_id["T1"]["status"] == "superseded"
    assert by_id["T1"]["superseded_by"] == "T2"
    assert by_id["T1"]["judge_reason"] == "implementation pivoted to the new route"
    assert by_id["T2"]["title"] == "New route is covered"
    assert by_id["T2"]["check"] == "rg new-route src/app.py"
    assert by_id["T2"]["added_by"] == "judge"
    assert by_id["T2"]["reconcile_evidence_refs"] == ["new-route exists"]
    assert any("T1 superseded by T2" in h for h in headlines)


def test_reconcile_revises_open_task_and_carries_evidence():
    spec = {"requires_tasks": True, "tasks": [_task("T1", "Old route still exists", "failed")]}
    evidence = {"command_outputs": ["pytest tests/test_route.py -q -> route now lives under /new-route"]}
    actions = [
        {
            "action": "revise",
            "id": "T1",
            "title": "New route is exposed",
            "check": "pytest tests/test_route.py -q",
            "reason": "captured test output shows the route moved",
            "evidence_refs": ["route now lives under /new-route"],
        }
    ]
    headlines = _apply_reconcile_actions(spec, actions, evidence=evidence)
    task = spec["tasks"][0]
    assert task["title"] == "New route is exposed"
    assert task["check"] == "pytest tests/test_route.py -q"
    assert task["status"] == "pending"
    assert task["judge_reason"] == "captured test output shows the route moved"
    assert task["reconcile_evidence_refs"] == ["route now lives under /new-route"]
    assert task["_check_stale"] is True
    assert task["_revise_this_stop"] is True
    assert headlines == ["T1 revised: captured test output shows the route moved"]


def test_reconcile_revise_headline_includes_full_long_reason():
    long_reason = (
        "The requirement is valid but its wording can be tightened to reflect "
        "the mechanism used in the reconciled implementation path."
    )
    assert len(long_reason) > 80
    spec = {"requires_tasks": True, "tasks": [_task("T1", "Old route still exists", "failed")]}
    evidence = {"command_outputs": ["pytest tests/test_route.py -q -> route now lives under /new-route"]}
    actions = [
        {
            "action": "revise",
            "id": "T1",
            "reason": long_reason,
            "evidence_refs": ["route now lives under /new-route"],
        }
    ]
    headlines = _apply_reconcile_actions(spec, actions, evidence=evidence)
    assert headlines == [f"T1 revised: {long_reason}"]


def test_post_tool_reconciliation_updates_persisted_spec(monkeypatch, tmp_path):
    os.environ["UNIFABLE_DATA"] = str(tmp_path)
    target = tmp_path / "src.txt"
    target.write_text("new-route\n", encoding="utf-8")
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "Remove the old route"
    spec["tasks"] = [_task("T1", "Old route still exists", "failed")]
    save_spec(str(tmp_path), "reconcile-post", spec)

    def fake_ask(_system, _user, _schema, schema_name=""):
        assert schema_name == "reconcile_spec"
        return {
            "actions": [
                {
                    "action": "retract",
                    "id": "T1",
                    "reason": "captured read shows the old route is gone",
                    "evidence_refs": [str(target)],
                }
            ]
        }

    import gate_post_tool
    import judge_transport

    payload = {
        "session_id": "reconcile-post",
        "turn_id": "turn-reconcile",
        "cwd": str(tmp_path),
        "tool_name": "Read",
        "tool_input": {"file_path": str(target)},
        "tool_response": {"content": target.read_text(encoding="utf-8")},
    }
    monkeypatch.setattr(judge_transport, "ask_structured", fake_ask)
    with patch.object(gate_post_tool, "read_stdin_json", lambda: payload):
        with patch("posttool_notify.emit_json"):
            gate_post_tool.main()
    saved = load_spec(str(tmp_path), "reconcile-post")
    assert saved["tasks"][0]["status"] == "retracted"
    assert saved["tasks"][0]["judge_reason"] == "captured read shows the old route is gone"
    assert saved["tasks"][0]["reconcile_evidence_refs"] == [str(target)]


def test_judge_reconcile_sends_captured_evidence(monkeypatch):
    captured: dict = {}

    def fake_ask(system, user, schema, schema_name=""):
        captured["system"] = system
        captured["user"] = user
        captured["schema"] = schema
        assert schema_name == "reconcile_spec"
        return {"actions": []}

    import judge_transport

    monkeypatch.setattr(judge_transport, "ask_structured", fake_ask)
    spec = {"restated_goal": "g", "tasks": [_task("T1")]}
    judge_reconcile_spec(spec, {"command_outputs": ["rg old -> 0 matches"]})
    assert "lifecycle changes are judge-owned" in captured["system"]
    assert "rg old -> 0 matches" in captured["user"]
