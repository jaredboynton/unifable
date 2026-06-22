#!/usr/bin/env python3
"""PostToolUse groundedness breaker release path (gate_post_tool.py integration)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "scripts" / "gate"))

from breaker_state import default_breaker, load_breaker, save_breaker  # noqa: E402


class ScriptedReleaseJudge:
    def __init__(
        self,
        grounded: int = 1,
        needed: str = "",
        provisional_release: int = 0,
        lift_reason: str = "",
        lift_scope: str = "",
    ):
        self.grounded = grounded
        self.needed = needed
        self.provisional_release = provisional_release
        self.lift_reason = lift_reason
        self.lift_scope = lift_scope
        self.calls = 0

    def __call__(self, system, user, schema):
        self.calls += 1
        if "provisional-lift monitor" in system.lower():
            return {"on_track": 1, "corrective": ""}
        lb = 1
        if self.grounded:
            return {
                "grounded": 1, "needed": "", "load_bearing": lb,
                "provisional_release": 0, "lift_reason": "", "lift_scope": "",
            }
        if self.provisional_release:
            return {
                "grounded": 0, "needed": "", "load_bearing": lb,
                "provisional_release": 1,
                "lift_reason": self.lift_reason,
                "lift_scope": self.lift_scope,
            }
        return {
            "grounded": 0, "needed": self.needed, "load_bearing": lb,
            "provisional_release": 0, "lift_reason": "", "lift_scope": "",
        }


def _armed_breaker(data_dir: str, sess: str, cwd: str, claim: str = "unproven root cause"):
    os.environ["UNIFABLE_DATA"] = data_dir
    payload = {"session_id": sess, "cwd": cwd}
    state = default_breaker()
    state["breaker_armed"] = True
    state["breaker_claim"] = claim
    state["breaker_steering"] = "read the source"
    state["events"] = [
        {
            "kind": "ARM",
            "ts": "2026-01-01T00:00:00+00:00",
            "claim": claim,
            "steering": "read the source",
        }
    ]
    save_breaker(payload, state)


def _run_post_tool(payload: dict, judge: ScriptedReleaseJudge | None = None):
    import gate_post_tool
    import groundedness

    if judge is not None:
        with patch.object(groundedness, "_default_judge", judge):
            with patch.object(gate_post_tool, "read_stdin_json", lambda: payload):
                with patch.object(gate_post_tool, "emit_json") as emit:
                    rc = gate_post_tool.main()
                    assert emit.call_count == 1
                    return rc, emit.call_args[0][0]
    with patch.object(gate_post_tool, "read_stdin_json", lambda: payload):
        with patch.object(gate_post_tool, "emit_json") as emit:
            rc = gate_post_tool.main()
            if emit.call_count:
                return rc, emit.call_args[0][0]
            return rc, {}


def test_post_tool_disarms_and_emits_breaker_open():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "PT1"
        _armed_breaker(dd, sess, cwd)
        payload = {
            "session_id": sess,
            "cwd": cwd,
            "tool_name": "Read",
            "tool_input": {"file_path": str(Path(cwd) / "foo.py")},
            "tool_response": {"content": "the hook reads transcript tail"},
        }
        judge = ScriptedReleaseJudge(grounded=1)
        rc, out = _run_post_tool(payload, judge)
        assert rc == 0
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        assert "breaker open" in ctx.lower()
        state = load_breaker({"session_id": sess, "cwd": cwd})
        assert state["breaker_armed"] is False
        assert any(e.get("kind") == "DISARM" for e in state["events"])
        assert judge.calls == 1


def test_post_tool_stays_armed_when_release_judge_says_no():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "PT2"
        _armed_breaker(dd, sess, cwd)
        judge = ScriptedReleaseJudge(
            grounded=0, needed="read groundedness.py:1 and cite the release path"
        )
        payload = {
            "session_id": sess,
            "cwd": cwd,
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://example.com/doc"},
            "tool_response": {"content": "not relevant"},
        }
        rc, out = _run_post_tool(payload, judge)
        assert rc == 0
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        assert "still armed" in ctx.lower()
        state = load_breaker({"session_id": sess, "cwd": cwd})
        assert state["breaker_armed"] is True
        assert state["breaker_steering"] == "read groundedness.py:1 and cite the release path"
        assert any(e.get("kind") == "NEEDED" for e in state["events"])


def test_post_tool_emits_provisional_lift_context():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "PT4"
        _armed_breaker(dd, sess, cwd)
        judge = ScriptedReleaseJudge(
            grounded=0,
            provisional_release=1,
            lift_reason="Pursuing baseline verification.",
            lift_scope="Edit config only.",
        )
        payload = {
            "session_id": sess,
            "cwd": cwd,
            "tool_name": "Read",
            "tool_input": {"file_path": str(Path(cwd) / "foo.py")},
            "tool_response": {"content": "baseline scores"},
        }
        rc, out = _run_post_tool(payload, judge)
        assert rc == 0
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        assert "provisional lift" in ctx.lower()
        state = load_breaker({"session_id": sess, "cwd": cwd})
        assert state["breaker_provisional"] is True
        assert any(e.get("kind") == "LIFT" for e in state["events"])


def test_post_tool_skips_release_when_disarmed():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "PT3"
        os.environ["UNIFABLE_DATA"] = dd
        payload = {
            "session_id": sess,
            "cwd": cwd,
            "tool_name": "Read",
            "tool_input": {"file_path": str(Path(cwd) / "foo.py")},
            "tool_response": {"content": "data"},
        }
        rc, out = _run_post_tool(payload)
        assert rc == 0
        assert out == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
