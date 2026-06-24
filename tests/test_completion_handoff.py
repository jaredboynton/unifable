#!/usr/bin/env python3
"""Tests for completion_handoff.py Stop handoff judge."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import completion_handoff  # noqa: E402
import gate_stop  # noqa: E402


def _write_transcript(path: Path, content: list[dict]) -> None:
    path.write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}})
        + "\n",
        encoding="utf-8",
    )


def _payload(transcript: Path, *, session_id: str = "handoff1", stop_hook_active: bool = False) -> dict:
    return {
        "session_id": session_id,
        "cwd": "/tmp",
        "transcript_path": str(transcript),
        "stop_hook_active": stop_hook_active,
    }


def test_blocks_want_me_to_investigate():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        transcript = Path(td) / "session.jsonl"
        text = (
            "The codex:unifable cell is worth a closer look. "
            "Want me to read the codex-unifable transcript to see whether those turns "
            "were productive grounding or pathological retries?"
        )
        _write_transcript(transcript, [{"type": "text", "text": text}])
        payload = _payload(transcript)

        def fake_judge(*_a, **_k):
            return {
                "ok_to_stop": False,
                "reason": "Agent asked permission to read a transcript it could read itself.",
                "steering": "Read the codex-unifable transcript and report findings.",
            }

        old_env = os.environ.get("UNIFABLE_DATA")
        try:
            os.environ["UNIFABLE_DATA"] = dd
            with patch.object(completion_handoff, "judge_completion_handoff", fake_judge):
                out = completion_handoff.completion_handoff_decision(payload, td)
        finally:
            if old_env is None:
                os.environ.pop("UNIFABLE_DATA", None)
            else:
                os.environ["UNIFABLE_DATA"] = old_env

        assert out and out.get("decision") == "block"
        assert "unresolved handoff" in out.get("reason", "")
        assert "Read the codex-unifable transcript" in out.get("reason", "")


def test_blocks_say_the_word_deferral():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        transcript = Path(td) / "session.jsonl"
        text = (
            "If you want a defensible number, say the word and I'll run 5x each "
            "and report medians."
        )
        _write_transcript(transcript, [{"type": "text", "text": text}])
        payload = _payload(transcript)

        def fake_judge(*_a, **_k):
            return {
                "ok_to_stop": False,
                "reason": "Agent deferred a benchmark run awaiting user permission.",
                "steering": "Run 5x each configuration and report medians.",
            }

        old_env = os.environ.get("UNIFABLE_DATA")
        try:
            os.environ["UNIFABLE_DATA"] = dd
            with patch.object(completion_handoff, "judge_completion_handoff", fake_judge):
                out = completion_handoff.completion_handoff_decision(payload, td)
        finally:
            if old_env is None:
                os.environ.pop("UNIFABLE_DATA", None)
            else:
                os.environ["UNIFABLE_DATA"] = old_env

        assert out and out.get("decision") == "block"
        assert "report medians" in out.get("reason", "")


def test_blocks_promise_without_tool():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        transcript = Path(td) / "session.jsonl"
        _write_transcript(
            transcript,
            [{"type": "text", "text": "I'll now implement the fix and run tests."}],
        )
        payload = _payload(transcript)

        def fake_judge(*_a, **_k):
            return {
                "ok_to_stop": False,
                "reason": "Promised implementation without tool calls.",
                "steering": "Implement the fix and run tests now.",
            }

        old_env = os.environ.get("UNIFABLE_DATA")
        try:
            os.environ["UNIFABLE_DATA"] = dd
            with patch.object(completion_handoff, "judge_completion_handoff", fake_judge):
                out = completion_handoff.completion_handoff_decision(payload, td)
        finally:
            if old_env is None:
                os.environ.pop("UNIFABLE_DATA", None)
            else:
                os.environ["UNIFABLE_DATA"] = old_env

        assert out and out.get("decision") == "block"


def test_allows_genuine_user_choice():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        transcript = Path(td) / "session.jsonl"
        _write_transcript(
            transcript,
            [{"type": "text", "text": "Would you like option A or B for the schema?"}],
        )
        payload = _payload(transcript)

        def fake_judge(*_a, **_k):
            return {
                "ok_to_stop": True,
                "reason": "Genuine user-owned architecture choice.",
                "blocked_on_user_only": True,
            }

        old_env = os.environ.get("UNIFABLE_DATA")
        try:
            os.environ["UNIFABLE_DATA"] = dd
            with patch.object(completion_handoff, "judge_completion_handoff", fake_judge):
                out = completion_handoff.completion_handoff_decision(payload, td)
        finally:
            if old_env is None:
                os.environ.pop("UNIFABLE_DATA", None)
            else:
                os.environ["UNIFABLE_DATA"] = old_env

        assert out is None


def test_allows_commit_permission():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        transcript = Path(td) / "session.jsonl"
        _write_transcript(
            transcript,
            [{"type": "text", "text": "Want me to commit these changes?"}],
        )
        payload = _payload(transcript)

        def fake_judge(*_a, **_k):
            return {
                "ok_to_stop": True,
                "reason": "Commit requires explicit user approval per policy.",
                "blocked_on_user_only": True,
            }

        old_env = os.environ.get("UNIFABLE_DATA")
        try:
            os.environ["UNIFABLE_DATA"] = dd
            with patch.object(completion_handoff, "judge_completion_handoff", fake_judge):
                out = completion_handoff.completion_handoff_decision(payload, td)
        finally:
            if old_env is None:
                os.environ.pop("UNIFABLE_DATA", None)
            else:
                os.environ["UNIFABLE_DATA"] = old_env

        assert out is None


def test_allows_when_last_turn_had_tool():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        transcript = Path(td) / "session.jsonl"
        _write_transcript(
            transcript,
            [
                {"type": "text", "text": "I'll now run the check."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}},
            ],
        )
        payload = _payload(transcript)
        called = {"judge": False}

        def fake_judge(*_a, **_k):
            called["judge"] = True
            return {"ok_to_stop": False, "reason": "should not run"}

        old_env = os.environ.get("UNIFABLE_DATA")
        try:
            os.environ["UNIFABLE_DATA"] = dd
            with patch.object(completion_handoff, "judge_completion_handoff", fake_judge):
                out = completion_handoff.completion_handoff_decision(payload, td)
        finally:
            if old_env is None:
                os.environ.pop("UNIFABLE_DATA", None)
            else:
                os.environ["UNIFABLE_DATA"] = old_env

        assert out is None
        assert not called["judge"]


def test_bypasses_stop_hook_active():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        transcript = Path(td) / "session.jsonl"
        _write_transcript(
            transcript,
            [{"type": "text", "text": "I'll now implement the fix and run tests."}],
        )
        payload = _payload(transcript, stop_hook_active=True)

        def fake_judge(*_a, **_k):
            return {
                "ok_to_stop": False,
                "reason": "Deferred work.",
                "steering": "Implement now.",
            }

        old_env = os.environ.get("UNIFABLE_DATA")
        try:
            os.environ["UNIFABLE_DATA"] = dd
            with patch.object(completion_handoff, "judge_completion_handoff", fake_judge):
                out = completion_handoff.completion_handoff_decision(payload, td)
        finally:
            if old_env is None:
                os.environ.pop("UNIFABLE_DATA", None)
            else:
                os.environ["UNIFABLE_DATA"] = old_env

        assert out and out.get("decision") == "block"


def test_cap_allows_after_n_blocks():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        transcript = Path(td) / "session.jsonl"
        _write_transcript(transcript, [{"type": "text", "text": "Want me to investigate?"}])
        payload = _payload(transcript)

        def fake_judge(*_a, **_k):
            return {"ok_to_stop": False, "reason": "defer", "steering": "go"}

        old_env = os.environ.get("UNIFABLE_DATA")
        old_cap = completion_handoff.COMPLETION_HANDOFF_BLOCK_CAP
        try:
            os.environ["UNIFABLE_DATA"] = dd
            completion_handoff.COMPLETION_HANDOFF_BLOCK_CAP = 2
            with patch.object(completion_handoff, "judge_completion_handoff", fake_judge):
                assert completion_handoff.completion_handoff_decision(payload, td)
                assert completion_handoff.completion_handoff_decision(payload, td)
                out = completion_handoff.completion_handoff_decision(payload, td)
        finally:
            completion_handoff.COMPLETION_HANDOFF_BLOCK_CAP = old_cap
            if old_env is None:
                os.environ.pop("UNIFABLE_DATA", None)
            else:
                os.environ["UNIFABLE_DATA"] = old_env

        assert out and out.get("systemMessage")
        assert "block cap reached" in out.get("systemMessage", "")


def test_fail_open_on_judge_error():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as dd:
        transcript = Path(td) / "session.jsonl"
        _write_transcript(transcript, [{"type": "text", "text": "Want me to investigate?"}])
        payload = _payload(transcript)

        def fake_judge(*_a, **_k):
            raise RuntimeError("judge down")

        old_env = os.environ.get("UNIFABLE_DATA")
        try:
            os.environ["UNIFABLE_DATA"] = dd
            with patch.object(completion_handoff, "judge_completion_handoff", fake_judge):
                out = completion_handoff.completion_handoff_decision(payload, td)
        finally:
            if old_env is None:
                os.environ.pop("UNIFABLE_DATA", None)
            else:
                os.environ["UNIFABLE_DATA"] = old_env

        assert out is None


def test_gate_stop_wires_handoff_before_loop_guard(tmp_path, monkeypatch):
    captured: dict = {}

    def _capture(data: dict) -> None:
        captured["out"] = data

    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [{"type": "text", "text": "Want me to read the transcript?"}],
    )

    def fake_decision(input_data, cwd):
        return {
            "decision": "block",
            "reason": "Stop blocked: unresolved handoff — do the offered work now.",
            "_handoff_steering": "Read the transcript.",
        }

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("UNIFABLE_GRADE", "LIGHT")
    gate_stop.read_stdin_json = lambda: {
        "session_id": "sess",
        "cwd": str(tmp_path),
        "transcript_path": str(transcript),
        "stop_hook_active": True,
    }
    gate_stop.emit_json = _capture
    with patch.object(completion_handoff, "completion_handoff_decision", fake_decision):
        gate_stop.main()

    out = captured.get("out") or {}
    assert out.get("decision") == "block"
    assert "unresolved handoff" in out.get("reason", "")
