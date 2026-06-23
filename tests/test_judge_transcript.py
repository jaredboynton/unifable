#!/usr/bin/env python3
"""Requirement-validation judge receives session transcript context."""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    _judge_system_for_task,
    _judge_system_with_transcript,
    _judge_user,
    _render_judge_transcript,
    auto_validate_spec,
    load_spec,
    save_spec,
    spec_template,
)
from transcript_tail import JUDGE_EFFECTIVE_MAX_CHARS  # noqa: E402


def _task(tid, status, check="true", title=None):
    return {"id": tid, "title": title or tid, "check": check, "status": status}


def _write_transcript(path: Path, marker: str) -> None:
    line = json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": marker}]},
    })
    path.write_text(line + "\n", encoding="utf-8")


def test_render_judge_transcript_from_path(tmp_path):
    marker = "UNIFABLE_JUDGE_TRANSCRIPT_MARKER_XYZ"
    tx = tmp_path / "session.jsonl"
    _write_transcript(tx, marker)
    out = _render_judge_transcript(str(tx))
    assert marker in out


def test_render_judge_transcript_empty_when_missing():
    assert _render_judge_transcript(None) == ""
    assert _render_judge_transcript("/no/such/file.jsonl") == ""


def test_judge_system_includes_transcript_not_user_payload():
    """Transcript rides the system prompt; user JSON stays task-focused."""
    transcript = "tool_result: pytest passed\nStop hook feedback: breaker CLOSED"
    system = _judge_system_with_transcript(_judge_system_for_task(_task("T1", "pending")), transcript)
    assert "SESSION TRANSCRIPT" in system
    assert "pytest passed" in system
    assert len(system) <= JUDGE_EFFECTIVE_MAX_CHARS

    spec = spec_template()
    spec["restated_goal"] = "g"
    spec["tasks"] = [_task("T1", "pending")]
    user = _judge_user(spec, spec["tasks"][0], 0, "check output")
    payload = json.loads(user)
    assert "transcript" not in payload
    assert "pytest passed" not in user


def test_auto_validate_passes_rendered_transcript_to_judge_tasks(tmp_path, monkeypatch):
    marker = "AUTO_VALIDATE_TRANSCRIPT_MARKER_ABC"
    tx = tmp_path / "t.jsonl"
    _write_transcript(tx, marker)

    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "K", s)

    captured: dict[str, str] = {}

    def fake_judge_tasks(sp, items, *, transcript=""):
        captured["transcript"] = transcript
        return [(1, "ok", [], "") for _ in items]

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_tasks", fake_judge_tasks)

    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path), transcript_path=str(tx))
    assert marker in captured.get("transcript", "")


def test_auto_validate_no_transcript_when_path_absent(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "K", s)

    captured: dict[str, str] = {}

    def fake_judge_tasks(sp, items, *, transcript=""):
        captured["transcript"] = transcript
        return [(1, "ok", [], "") for _ in items]

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_tasks", fake_judge_tasks)

    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert captured.get("transcript") == ""


def test_auto_validate_transcript_reaches_judge_system(tmp_path, monkeypatch):
    """Transport-level: transcript tail is appended to judge system instructions."""
    import codex_judge

    marker = "TRANSPORT_SYSTEM_MARKER_QRS"
    tx = tmp_path / "t.jsonl"
    _write_transcript(tx, marker)

    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "K", s)

    captured: dict[str, str] = {}

    def fake_ask(system, user, schema, **kw):
        captured["system"] = system
        return {"verdict": 1, "reason": "ok"}

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(codex_judge, "ask_structured", fake_ask)

    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path), transcript_path=str(tx))
    assert marker in captured.get("system", "")
    assert marker not in captured.get("user", "")
