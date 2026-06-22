#!/usr/bin/env python3
"""Spec CLI model notifications: stderr prefixes and PostToolUse forwarding."""

from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import model_notify as mn  # noqa: E402
from spec import save_spec, spec_template, _cmd_status  # noqa: E402


def _sample_spec(*, judge_reason: str = "") -> dict:
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "Add density reinforcement to prompt adaptations"
    spec["tasks"] = [
        {
            "id": "T1",
            "title": "Density reinforcement lines added",
            "check": "true",
            "status": "failed",
            "judge_reason": judge_reason,
        },
        {"id": "T2", "title": "Re-measure flash-lite", "check": "true", "status": "pending"},
        {"id": "T4", "title": "Verify capsule floor", "check": "true", "status": "pending", "added_by": "judge"},
    ]
    return spec


LONG_JUDGE = (
    "The passing conflict scan is good evidence that stance conflicts were not detected, "
    "but it is not sufficient evidence that density reinforcement was actually added."
)


def test_format_spec_status_shows_board_and_highlight_judge():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    text = mn.format_spec_status(spec, highlight_task="T1")
    assert "goal: Add density reinforcement" in text
    assert "[XX] T1 Density reinforcement" in text
    assert "[--] T4 Verify capsule floor" in text
    assert f"judge: {LONG_JUDGE}" in text
    assert "breaker: CLOSED" in text


def test_notify_spec_update_emits_prefixes_and_full_judge():
    spec = _sample_spec()
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(
            spec,
            "T1 check ran (exit 2); judge rejected the evidence.",
            highlight_task="T1",
            judge_reason=LONG_JUDGE,
        )
    err = buf.getvalue()
    assert mn.NOTIFY_PREFIX in err
    assert mn.STATUS_PREFIX in err
    assert mn.JUDGE_PREFIX in err
    assert LONG_JUDGE in err
    assert "T4 Verify capsule floor" in err.replace("\\n", "\n")


def test_build_spec_context_from_output_roundtrip():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(
            spec,
            "T1 check ran (exit 2); judge rejected the evidence. Judge added T4.",
            highlight_task="T1",
            judge_reason=LONG_JUDGE,
        )
    combined = "stdout noise\n" + buf.getvalue()
    ctx = mn.build_spec_context_from_output(combined)
    assert ctx.startswith("unifable spec update:")
    assert "judge rejected the evidence" in ctx
    assert f"Judge: {LONG_JUDGE}" in ctx
    assert "[--] T4" in ctx
    assert "breaker: CLOSED" in ctx


def test_parse_spec_cli_invocation():
    sub, tid = mn.parse_spec_cli_invocation(
        "unifable-spec validate-task --task-id abc-123 --task T1"
    )
    assert sub == "validate-task"
    assert tid == "abc-123"


def _run_post_tool(payload: dict) -> dict:
    import gate_post_tool

    with patch.object(gate_post_tool, "read_stdin_json", lambda: payload):
        with patch.object(gate_post_tool, "emit_json") as emit:
            gate_post_tool.main()
            if emit.call_count:
                return emit.call_args[0][0]
            return {}


def test_post_tool_forwards_failed_validate_task_stderr():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(
            spec,
            "T1 check ran (exit 2); judge rejected the evidence. Judge added T4, T5.",
            highlight_task="T1",
            judge_reason=LONG_JUDGE,
        )
    stderr = buf.getvalue()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        payload = {
            "session_id": "spec-notify-test",
            "cwd": tmp,
            "tool_name": "Bash",
            "tool_input": {
                "command": "unifable-spec validate-task --task-id sess-1 --task T1",
            },
            "tool_response": {
                "exit_code": 2,
                "stdout": "T1 -> failed\njudge added requirement(s): T4, T5",
                "stderr": stderr,
            },
        }
        out = _run_post_tool(payload)
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "unifable spec update:" in ctx
    assert "judge rejected the evidence" in ctx
    assert LONG_JUDGE in ctx
    assert "T4" in ctx
    assert "breaker: CLOSED" in ctx
    assert "observed a tool failure" not in ctx


def test_post_tool_reload_fallback_when_stderr_missing():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        spec = _sample_spec(judge_reason=LONG_JUDGE)
        save_spec(tmp, "sess-reload", spec)
        payload = {
            "session_id": "spec-reload-test",
            "cwd": tmp,
            "tool_name": "Bash",
            "tool_input": {
                "command": "unifable-spec add-task --task-id sess-reload --title x --check true",
            },
            "tool_response": {"exit_code": 0, "stdout": "Added T9: x"},
        }
        out = _run_post_tool(payload)
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        assert "unifable spec update:" in ctx
        assert "[XX] T1" in ctx
        assert "[--] T4" in ctx


def test_post_tool_add_task_success_no_failure_nag():
    spec = _sample_spec()
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(spec, "Requirement T9 added: new req.", highlight_task="T9")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        payload = {
            "session_id": "spec-add-test",
            "cwd": tmp,
            "tool_name": "Bash",
            "tool_input": {
                "command": "unifable-spec add-task --task-id sess-1 --title new --check true",
            },
            "tool_response": {"exit_code": 0, "stdout": "Added T9", "stderr": buf.getvalue()},
        }
        out = _run_post_tool(payload)
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "Requirement T9 added" in ctx
    assert "observed a tool failure" not in ctx


def test_status_exits_zero_when_breaker_closed(tmp_path):
    spec = _sample_spec()
    save_spec(str(tmp_path), "sess-status", spec)
    rc = _cmd_status(
        type("Args", (), {"root": str(tmp_path), "task_id": "sess-status"})()
    )
    assert rc == 0


def test_post_tool_status_injects_board_without_notify_stderr():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        spec = _sample_spec(judge_reason=LONG_JUDGE)
        save_spec(tmp, "sess-status", spec)
        payload = {
            "session_id": "spec-status-test",
            "cwd": tmp,
            "tool_name": "Bash",
            "tool_input": {
                "command": "unifable-spec status --task-id sess-status",
            },
            "tool_response": {
                "exit_code": 0,
                "stdout": mn.format_spec_status(spec),
                "stderr": "",
            },
        }
        out = _run_post_tool(payload)
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "unifable spec update:" in ctx
    assert "[XX] T1" in ctx
    assert "breaker: CLOSED" in ctx
    assert "observed a tool failure" not in ctx
