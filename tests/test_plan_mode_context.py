#!/usr/bin/env python3
"""Plan mode detection and judge/hook context wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import spec_stop_validate as ssv  # noqa: E402
from plan_mode import (  # noqa: E402
    append_plan_mode_note,
    detect_plan_mode,
    detect_plan_mode_from_prompt,
    empty_plan_mode,
    plan_mode_context_line,
    resolve_plan_mode,
)
from spec import auto_validate_spec, load_spec, save_spec, spec_template  # noqa: E402
from spec_judge import (
    _build_validate_all_user,  # noqa: E402
    _judge_context,
    _judge_system_for_task,
    _judge_system_with_transcript,
)
from transcript_tail import JUDGE_EFFECTIVE_MAX_CHARS  # noqa: E402


def _task(tid, status, check="true", title=None):
    return {"id": tid, "title": title or tid, "check": check, "status": status}


def _write_lines(path: Path, lines: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )


def test_detect_codex_plan_mode(tmp_path):
    tx = tmp_path / "codex.jsonl"
    _write_lines(
        tx,
        [
            {"type": "event_msg", "payload": {"type": "task_started", "collaboration_mode_kind": "plan"}},
            {"type": "turn_context", "payload": {"collaboration_mode": {"mode": "agent"}}},
            {"type": "turn_context", "payload": {"collaboration_mode": {"mode": "plan"}}},
        ],
    )
    pm = detect_plan_mode(str(tx))
    assert pm["enabled"] is True
    assert pm["host"] == "codex"


def test_detect_claude_plan_mode_lifecycle(tmp_path):
    tx = tmp_path / "claude.jsonl"
    _write_lines(
        tx,
        [
            {"type": "attachment", "attachment": {"type": "plan_mode", "isSubAgent": False}},
            {"type": "attachment", "attachment": {"type": "plan_mode_exit", "isSubAgent": False}},
            {"type": "attachment", "attachment": {"type": "plan_mode_reentry", "isSubAgent": False}},
            {"type": "attachment", "attachment": {"type": "plan_mode", "isSubAgent": False}},
            {"type": "attachment", "attachment": {"type": "plan_mode_exit", "isSubAgent": False}},
        ],
    )
    pm = detect_plan_mode(str(tx))
    assert pm["enabled"] is False
    assert pm["host"] == "claude"
    assert "plan_mode_exit" in pm["marker"]


def test_detect_claude_ignores_subagent(tmp_path):
    tx = tmp_path / "claude.jsonl"
    _write_lines(
        tx,
        [
            {"type": "attachment", "attachment": {"type": "plan_mode", "isSubAgent": True}},
        ],
    )
    assert detect_plan_mode(str(tx)) == empty_plan_mode()


def test_detect_cursor_switch_mode(tmp_path):
    tx = tmp_path / "cursor.jsonl"
    _write_lines(
        tx,
        [
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "SwitchMode",
                            "input": {"target_mode_id": "plan"},
                        }
                    ],
                },
            },
            {
                "role": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "SwitchMode",
                            "input": {"target_mode_id": "agent"},
                        }
                    ],
                },
            },
        ],
    )
    pm = detect_plan_mode(str(tx))
    assert pm["enabled"] is False
    assert pm["host"] == "cursor"


def test_detect_cursor_system_reminder_in_prompt():
    prompt = (
        "<system_reminder>\nPlan mode is active. The user indicated that they do not want you to execute yet.\n</system_reminder>"
    )
    pm = detect_plan_mode_from_prompt(prompt)
    assert pm["enabled"] is True
    assert pm["host"] == "cursor"


def test_resolve_plan_mode_prompt_when_no_transcript():
    prompt = "<system_reminder>Plan mode is active</system_reminder>"
    pm = resolve_plan_mode({"prompt": prompt}, transcript_path=None)
    assert pm["enabled"] is True
    assert pm["host"] == "cursor"


def test_detect_missing_transcript_fail_open():
    assert detect_plan_mode(None) == empty_plan_mode()
    assert detect_plan_mode("/no/such/file.jsonl") == empty_plan_mode()


def test_judge_system_includes_plan_section():
    pm = {"enabled": True, "host": "codex", "marker": "task_started:plan"}
    system = _judge_system_with_transcript(
        _judge_system_for_task(_task("T1", "pending"), plan_mode=pm),
        "tool output here",
        plan_mode=pm,
    )
    assert "PLAN MODE (codex)" in system
    assert "plan_mode_enabled" in system
    assert len(system) <= JUDGE_EFFECTIVE_MAX_CHARS


def test_validate_all_user_includes_session_context():
    spec = spec_template()
    spec["restated_goal"] = "g"
    spec["tasks"] = [_task("T1", "pending")]
    pm = {"enabled": True, "host": "claude", "marker": "attachment:plan_mode"}
    payload = json.loads(
        _build_validate_all_user(
            spec,
            [{"task": spec["tasks"][0], "kind": "validate", "exit_code": 1, "output": "missing"}],
            pm,
        )
    )
    assert payload["session_context"]["plan_mode_enabled"] is True
    assert payload["session_context"]["plan_mode_host"] == "claude"


def test_judge_context_returns_plan_mode(tmp_path):
    tx = tmp_path / "t.jsonl"
    _write_lines(
        tx,
        [
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "marker"}]}},
            {"type": "event_msg", "payload": {"type": "task_started", "collaboration_mode_kind": "plan"}},
        ],
    )
    _tail, pm = _judge_context(str(tx))
    assert pm["enabled"] is True
    assert pm["host"] == "codex"


def test_auto_validate_passes_plan_mode_to_judge_tasks(tmp_path, monkeypatch):
    tx = tmp_path / "t.jsonl"
    _write_lines(
        tx,
        [
            {"type": "event_msg", "payload": {"type": "task_started", "collaboration_mode_kind": "plan"}},
        ],
    )
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "K", s)

    captured: dict = {}

    def fake_judge_tasks(sp, items, *, transcript="", plan_mode=None, **_kw):
        captured["transcript"] = transcript
        captured["plan_mode"] = plan_mode
        return [(1, "ok", [], "") for _ in items]

    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks", fake_judge_tasks)

    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path), transcript_path=str(tx))
    assert captured["plan_mode"]["enabled"] is True
    assert captured["plan_mode"]["host"] == "codex"


def test_append_plan_mode_note():
    base = "write blocked"
    out = append_plan_mode_note(base, {"enabled": True, "host": "cursor"})
    assert "Plan Mode active" in out
    assert append_plan_mode_note(out, {"enabled": True, "host": "cursor"}) == out


def test_plan_mode_context_line_when_disabled():
    assert plan_mode_context_line({"enabled": False}) == ""
