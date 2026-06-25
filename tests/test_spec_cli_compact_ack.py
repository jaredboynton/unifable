#!/usr/bin/env python3
"""Successful mutating spec CLI commands ack via stdout only."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import model_notify as mn  # noqa: E402
from spec import save_spec, spec_template  # noqa: E402


def _run_post_tool(payload: dict) -> dict:
    import gate_post_tool

    with patch.object(gate_post_tool, "read_stdin_json", lambda: payload):
        with patch("posttool_notify.emit_json") as emit:
            gate_post_tool.main()
            if emit.call_count:
                return emit.call_args[0][0]
            return {}


def _ctx(payload: dict) -> str:
    out = _run_post_tool(payload)
    return (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""


def test_add_task_success_is_silent():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    spec["tasks"] = []
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        save_spec(tmp, "add-task", spec)
        ctx = _ctx(
            {
                "session_id": "add-task",
                "cwd": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "unifable add-task --title req --check true"},
                "tool_response": {"exit_code": 0, "stdout": "Added T1: req"},
            }
        )
        assert ctx == ""


def test_set_primary_success_is_silent():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["heavy_workflow"] = True
    spec["tasks"] = [
        {
            "id": "T1",
            "title": "frontier",
            "check": "true",
            "status": "pending",
            "approach_kind": "frontier",
        },
        {
            "id": "T2",
            "title": "frontier b",
            "check": "true",
            "status": "pending",
            "approach_kind": "frontier",
        },
    ]
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        save_spec(tmp, "set-primary", spec)
        ctx = _ctx(
            {
                "session_id": "set-primary",
                "cwd": tmp,
                "tool_name": "Bash",
                "tool_input": {
                    "command": "unifable set-primary --title primary --check true",
                },
                "tool_response": {
                    "exit_code": 0,
                    "stdout": "Primary approach set: T3 (blocked until frontiers ruled out).",
                },
            }
        )
        assert ctx == ""


def test_add_frontier_success_is_silent():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["heavy_workflow"] = True
    spec["tasks"] = []
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        save_spec(tmp, "add-frontier", spec)
        ctx = _ctx(
            {
                "session_id": "add-frontier",
                "cwd": tmp,
                "tool_name": "Bash",
                "tool_input": {
                    "command": "unifable add-frontier --title path --check true",
                },
                "tool_response": {
                    "exit_code": 0,
                    "stdout": "Frontier approach added: T1 (1 total).",
                },
            }
        )
        assert ctx == ""


def test_dispute_success_is_silent():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["tasks"] = [
        {"id": "T1", "title": "req", "check": "true", "status": "failed"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        save_spec(tmp, "dispute", spec)
        ctx = _ctx(
            {
                "session_id": "dispute",
                "cwd": tmp,
                "tool_name": "Bash",
                "tool_input": {
                    "command": "unifable dispute T1 --evidence impossible",
                },
                "tool_response": {
                    "exit_code": 0,
                    "stdout": "T1 -> disputed. The harness adjudicates impossibility/obsolescence claims on stop.",
                },
            }
        )
        assert ctx == ""


def test_restate_with_no_tasks_emits_add_task_only():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "ship feature"
    spec["tasks"] = []
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        save_spec(tmp, "restate", spec)
        ctx = _ctx(
            {
                "session_id": "restate",
                "cwd": tmp,
                "tool_name": "Bash",
                "tool_input": {"command": "unifable restate 'ship feature'"},
                "tool_response": {
                    "exit_code": 0,
                    "stdout": "restated_goal set (12 chars); goal_seeded cleared.",
                },
            }
        )
        assert ctx.startswith("Add at least one:")
        assert "unifable add-task" in ctx
        assert "Goal restated" not in ctx


def test_notify_spec_update_stdout_only_emits_nothing(capsys):
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["tasks"] = []
    mn.notify_spec_update(spec, "Requirement T1 added: x.", surface="stdout_only")
    assert capsys.readouterr().err == ""


def test_parse_mutating_spec_stdout():
    assert mn.parse_mutating_spec_stdout("add-task", "Added T2: title") == "T2"
    assert mn.parse_mutating_spec_stdout("set-primary", "Primary approach set: T3 (blocked).") == "T3"
    assert mn.parse_mutating_spec_stdout("restate", "restated_goal set (1 chars); goal_seeded cleared.") == "restate"
