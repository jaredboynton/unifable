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
from spec import save_spec, spec_template  # noqa: E402


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
    assert "[XX] T1 (req) Density reinforcement" in text
    assert "[--] T4 (req) Verify capsule floor" in text
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
    assert "T4 (req) Verify capsule floor" in err.replace("\\n", "\n")


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


HINT = "Run `unifable-spec where` -- the spec key looks fragmented; converge on one spec before validating."


def test_format_spec_status_shows_advisory_hint_on_highlight():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    spec["tasks"][0]["judge_hint"] = HINT
    text = mn.format_spec_status(spec, highlight_task="T1")
    assert f"hint (advisory, not a gate): {HINT}" in text
    # a non-highlighted task does not leak its hint
    spec["tasks"][1]["judge_hint"] = "other hint"
    text2 = mn.format_spec_status(spec, highlight_task="T1")
    assert "other hint" not in text2


def test_notify_spec_update_emits_hint_prefix():
    spec = _sample_spec()
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(
            spec,
            "T1 check ran (exit 2); judge rejected the evidence.",
            highlight_task="T1",
            judge_reason=LONG_JUDGE,
            hint=HINT,
        )
    err = buf.getvalue()
    assert mn.HINT_PREFIX in err
    assert HINT in err


def test_build_spec_context_includes_advisory_hint():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(
            spec, "T1 rejected.", highlight_task="T1", judge_reason=LONG_JUDGE, hint=HINT
        )
    ctx = mn.build_spec_context_from_output("noise\n" + buf.getvalue())
    assert f"Hint (advisory, not a gate): {HINT}" in ctx


def test_notify_spec_update_omits_hint_when_empty():
    spec = _sample_spec()
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(spec, "T1 validated.", highlight_task="T1")
    assert mn.HINT_PREFIX not in buf.getvalue()


def test_parse_spec_cli_invocation():
    sub, tid = mn.parse_spec_cli_invocation("unifable add-task --title x --check true")
    assert sub == "add-task"
    assert tid is None


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
                "command": "unifable add-task --title x --check true",
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


def test_post_tool_add_task_reload_fallback_when_stderr_missing():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        spec = _sample_spec(judge_reason=LONG_JUDGE)
        save_spec(tmp, "sess-reload", spec)
        payload = {
            "session_id": "sess-reload",
            "cwd": tmp,
            "tool_name": "Bash",
            "tool_input": {
                "command": "unifable add-task --title x --check true",
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
                "command": "unifable add-task --title new --check true",
            },
            "tool_response": {"exit_code": 0, "stdout": "Added T9", "stderr": buf.getvalue()},
        }
        out = _run_post_tool(payload)
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "Requirement T9 added" in ctx
    assert "observed a tool failure" not in ctx
