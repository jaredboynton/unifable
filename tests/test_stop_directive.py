#!/usr/bin/env python3
"""T5: a blocked Stop carries the live director directive (guided-iterative-continuation).

A "turn" is one tool call; Stop (end-of-session) is rare and means the model thinks
it is done. When the gate blocks that Stop, it must hand back the live director
directive so the goal loop continues with the same per-tool-call guidance. A clean
allow-stop must NOT inject the directive (that would re-engage the session and
prevent legitimate completion).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for p in (str(REPO / "hooks"), str(REPO / "scripts" / "gate")):
    if p not in sys.path:
        sys.path.insert(0, p)

import breaker_state  # noqa: E402
import gate_stop  # noqa: E402


def _seed_directive(input_data: dict, directive: str) -> None:
    st = breaker_state.default_breaker()
    st["breaker_directive"] = directive
    breaker_state.save_breaker(input_data, st)


def test_blocked_stop_injects_live_directive(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    input_data = {"session_id": "stop-dir", "cwd": str(tmp_path)}
    _seed_directive(input_data, "Read scripts/gate/spec.py before editing the schema.")

    captured: dict = {}
    monkeypatch.setattr(gate_stop, "emit_json", lambda d: captured.update({"out": d}))
    gate_stop._emit_stop_payload(
        {"decision": "block", "reason": "evidence spec incomplete"},
        input_data,
        validate_ctx="=== board ===",
    )
    blob = json.dumps(captured.get("out", {}))
    assert "Read scripts/gate/spec.py before editing the schema." in blob


def test_allow_stop_does_not_inject_directive(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    input_data = {"session_id": "stop-allow", "cwd": str(tmp_path)}
    _seed_directive(input_data, "Read scripts/gate/spec.py first.")

    captured: dict = {}
    monkeypatch.setattr(gate_stop, "emit_json", lambda d: captured.update({"out": d}))
    gate_stop._emit_stop_payload({}, input_data)
    assert captured.get("out") == {}


def test_blocked_stop_no_directive_when_unset(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    input_data = {"session_id": "stop-empty", "cwd": str(tmp_path)}
    # No breaker directive seeded -> nothing extra to inject, must not crash.
    captured: dict = {}
    monkeypatch.setattr(gate_stop, "emit_json", lambda d: captured.update({"out": d}))
    gate_stop._emit_stop_payload(
        {"decision": "block", "reason": "blocked"}, input_data, validate_ctx="board"
    )
    assert captured.get("out", {}).get("decision") == "block"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
