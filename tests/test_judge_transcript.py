#!/usr/bin/env python3
"""Requirement-validation judge receives session transcript context."""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import spec_stop_validate as ssv  # noqa: E402
import transcript_tail  # noqa: E402
from spec import auto_validate_spec, load_spec, save_spec, spec_template  # noqa: E402
from spec_judge import (
    _judge_context,  # noqa: E402
    _judge_system_for_task,
    _judge_system_with_transcript,
    _judge_user,
    _render_judge_transcript,
)
from transcript_tail import JUDGE_EFFECTIVE_MAX_CHARS  # noqa: E402


def _task(tid, status, check="true", title=None):
    return {"id": tid, "title": title or tid, "check": check, "status": status}


def _write_transcript(path: Path, marker: str) -> None:
    line = json.dumps(
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": marker}]},
        }
    )
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
    transcript = "tool_result: pytest passed\nStop hook feedback: Completion gate blocked"
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

    def fake_judge_tasks(sp, items, *, transcript="", plan_mode=None, **_kw):
        captured["transcript"] = transcript
        return [(1, "ok", [], "") for _ in items]

    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks", fake_judge_tasks)

    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path), transcript_path=str(tx))
    assert marker in captured.get("transcript", "")


def test_auto_validate_no_transcript_when_path_absent(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "K", s)

    captured: dict[str, str] = {}

    def fake_judge_tasks(sp, items, *, transcript="", plan_mode=None, **_kw):
        captured["transcript"] = transcript
        return [(1, "ok", [], "") for _ in items]

    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks", fake_judge_tasks)

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

    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(codex_judge, "ask_structured", fake_ask)

    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path), transcript_path=str(tx))
    assert marker in captured.get("system", "")
    assert marker not in captured.get("user", "")


def _record_line(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})


def _prefix_changes(render, tmp_path, *, appends=60) -> int:
    """Count how many times a renderer's window prefix changes over a sequence of
    single-record appends. A sliding tail shifts on (nearly) every append; a sticky
    retention window only shifts when a chunk boundary is crossed."""
    tx = tmp_path / "session.jsonl"
    records = [_record_line("REC-%04d-PAYLOAD" % i) for i in range(400)]
    tx.write_text("\n".join(records) + "\n", encoding="utf-8")
    changes = 0
    prev = None
    for k in range(appends):
        head = render(str(tx))[:200]
        if prev is not None and head != prev:
            changes += 1
        prev = head
        records.append(_record_line("APP-%04d-PAYLOAD" % k))
        tx.write_text("\n".join(records) + "\n", encoding="utf-8")
    return changes


def test_retained_renderer_is_sticky_unlike_sliding_tail(tmp_path):
    """The retained renderer holds a byte-identical prompt-cache prefix across most
    appends (only stepping on ~800-char chunk boundaries), whereas the old sliding
    `stripped_transcript_tail` shifts its prefix on essentially every append and so
    busts gpt-realtime-2's prompt cache every turn."""
    from transcript_tail import (
        MAX_CHARS_PER_TOKEN,
        stripped_transcript_retained,
        stripped_transcript_tail,
    )

    retained = _prefix_changes(lambda p: stripped_transcript_retained(p, max_tokens=1000), tmp_path)
    sliding = _prefix_changes(lambda p: stripped_transcript_tail(p, max_tokens=1000), tmp_path)

    # 60 appends * ~70 chars / 800-char chunk -> ~5 boundary crossings for retention,
    # vs a prefix shift on (nearly) every append for the sliding tail.
    assert sliding >= 55
    assert retained <= 15
    assert retained < sliding

    # Always a bounded suffix of the full stripped transcript.
    out = stripped_transcript_retained(str(tmp_path / "session.jsonl"), max_tokens=1000)
    assert 0 < len(out) <= 1000 * MAX_CHARS_PER_TOKEN


def test_judge_context_transcript_prefix_is_sticky(tmp_path, monkeypatch):
    """End-to-end at the spec fix point: _judge_context feeds the requirement-
    validation judge a sticky transcript, so consecutive Stop validations share a
    cacheable prefix. With the prior sliding-tail renderer the prefix shifted every
    turn (this count would be ~60 instead of a handful)."""
    monkeypatch.setattr(transcript_tail, "TRANSCRIPT_TOKEN_BUDGET", 1000)
    changes = _prefix_changes(lambda p: _judge_context(p)[0], tmp_path)
    assert changes <= 15
