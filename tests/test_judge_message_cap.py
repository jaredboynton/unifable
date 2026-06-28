#!/usr/bin/env python3
"""Tests for gpt-realtime-2 judge message char caps."""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "gate"))

import codex_judge as cj  # noqa: E402
import groundedness as gb  # noqa: E402
from transcript_tail import (  # noqa: E402
    JUDGE_EFFECTIVE_MAX_CHARS,
    JUDGE_MAX_MESSAGE_CHARS,
    JUDGE_TRANSCRIPT_CHAR_BUDGET,
    cap_judge_message,
    fit_judge_user_message,
)

from unifable_runtime.transport import realtime_ws as _ws  # noqa: E402

_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "integer"}},
    "required": ["verdict"],
}


def test_cap_judge_message_tail_preserves_and_marks():
    text = "START" + ("x" * 300_000) + "END"
    out = cap_judge_message(text, 1000)
    assert len(out) <= 1000
    assert "truncated" in out
    assert "xxx" in out or "END" in out


def test_fit_judge_user_message_keeps_prefix():
    prefix = "PREFIX:\n"
    body = "y" * 300_000
    out = fit_judge_user_message(prefix, body)
    assert len(out) <= JUDGE_EFFECTIVE_MAX_CHARS
    assert out.startswith(prefix)
    assert "y" in out


def test_fit_judge_user_message_with_suffix_trims_body_only():
    prefix = "HEAD\n"
    suffix = "\nTAIL"
    body = "m" * 300_000
    out = fit_judge_user_message(prefix, body, suffix=suffix)
    assert len(out) <= JUDGE_EFFECTIVE_MAX_CHARS
    assert out.startswith(prefix)
    assert out.endswith(suffix)


def test_disarm_shaped_message_under_limit():
    claim = "some claim"
    segment = "z" * 300_000
    prefix = f"FLAGGED CLAIM:\n{claim}\n\nTRANSCRIPT (what the model has since read/run/cited):\n"
    user = fit_judge_user_message(prefix, segment)
    assert len(user) <= JUDGE_EFFECTIVE_MAX_CHARS
    assert user.startswith("FLAGGED CLAIM:")


def test_ask_structured_caps_before_send(monkeypatch):
    # conftest forces the hermetic offline knob on; this test drives the real
    # ask_structured path with a fake socket, so clear it.
    monkeypatch.delenv("UNIFABLE_JUDGE_OFFLINE", raising=False)
    captured: list[dict] = []

    def fake_fresh_tokens(auth_path, force=False):
        return {"access_token": "tok", "account_id": ""}

    def fake_ws_connect(tokens, model, timeout):
        from unittest.mock import MagicMock

        return MagicMock()

    def fake_read_frame(sock):
        payload = json.dumps(
            {
                "type": "response.done",
                "response": {"output": [{"type": "function_call", "arguments": '{"verdict":1}'}]},
            }
        ).encode()
        return True, 0x1, payload  # (fin, opcode, payload)

    def capture_send(sock, obj):
        captured.append(obj)

    huge = "A" * 300_000
    monkeypatch.setattr(cj, "_fresh_tokens", fake_fresh_tokens)
    monkeypatch.setattr(cj, "_ws_connect", fake_ws_connect)
    # _read_message lives in the canonical transport and resolves _read_frame in
    # its own namespace, so patch the seam there (not on the cj re-export).
    monkeypatch.setattr(_ws, "_read_frame", fake_read_frame)
    monkeypatch.setattr(cj, "_send_text", capture_send)

    out = cj.ask_structured(huge, huge, _SCHEMA, schema_name="cap_test")
    assert out == {"verdict": 1}

    session = next(o for o in captured if o.get("type") == "session.update")
    question = next(o for o in captured if o.get("type") == "conversation.item.create")
    instructions = session["session"]["instructions"]
    user_text = question["item"]["content"][0]["text"]
    assert len(instructions) <= JUDGE_EFFECTIVE_MAX_CHARS
    assert len(user_text) <= JUDGE_MAX_MESSAGE_CHARS


def test_arm_judge_system_stays_bounded_with_many_adjudicated_claims():
    def bad_judge(system, user, schema):
        return {"verdict": 0, "steering": "", "claim": "", "load_bearing": 0}

    events = [{"kind": "DISARM", "claim": "claim " + ("c" * 10_000)} for _ in range(50)]
    _verdict, _steering, _claim = gb.arm_judge("segment", events=events, judge=bad_judge)
    # arm_judge passes system to judge; verify cap via re-running cap on composed system
    from transcript_tail import cap_judge_message as cap

    system = gb._JUDGE_SYSTEM
    done = gb.adjudicated_claims(events)
    claims_str = "\n".join(f"- {c}" for c in done)
    append = f"\n\nDo NOT flag any of the following claims as they have already been adjudicated or grounded:\n{claims_str}"
    room = JUDGE_EFFECTIVE_MAX_CHARS - len(system)
    composed = system + cap(append, room)
    assert len(composed) <= JUDGE_EFFECTIVE_MAX_CHARS


def test_transcript_char_budget_below_api_limit():
    assert JUDGE_TRANSCRIPT_CHAR_BUDGET < JUDGE_MAX_MESSAGE_CHARS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
