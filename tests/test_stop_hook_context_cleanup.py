#!/usr/bin/env python3
"""Stop-hook context cleanup: internal breaker state must not surface on the
allow-stop path, and loop-lift / runaway / hint copy must not narrate internals.

Covers:
  Stop-4  completion_runaway_warning is a terse one-line human-review nudge.
  Stop-5/8 provisional_allow_message / format_loop_lift_context omit the
          judge-internal loop_lift_reason (kept as an audit note only).
  Stop-6  allow-stop emits {} when only release/verify notes are pending;
          only warning_after_max_blocks reaches the model.
  Stop-11 _completion_stop_hint signal has no duplicated/garbled clause.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import loop_release as lr  # noqa: E402
import verify_state as vs  # noqa: E402


# Stop-4 ---------------------------------------------------------------------
def test_completion_runaway_warning_is_terse():
    msg = vs.completion_runaway_warning(3)
    # No internal narration about breaker mechanics.
    assert "adding requirements at least as fast" not in msg
    assert "RELEASED" not in msg
    assert "Surfacing for human review" not in msg
    # Terse, actionable, one line.
    assert "human review" in msg
    assert "3 requirement(s)" in msg
    assert msg.count("\n") == 0


# Stop-5 / Stop-8 ------------------------------------------------------------
def test_provisional_allow_message_omits_internal_reason():
    led = {
        "loop_lift_stops_remaining": 2,
        "loop_lift_scope": "fix the check",
        "loop_lift_reason": "Retract T16 because T15 covers it.",
    }
    msg = lr.provisional_allow_message(led)
    assert "Retract T16" not in msg
    assert "provisional Stop lift" in msg
    assert "fix the check" in msg
    assert "2" in msg


def test_format_loop_lift_context_omits_internal_reason():
    led = {
        "loop_lift_kind": "provisional",
        "loop_lift_stops_remaining": 1,
        "loop_lift_scope": "rewrite the check",
        "loop_lift_reason": "loop detected on T1",
    }
    msg = lr.format_loop_lift_context(led)
    assert "loop detected on T1" not in msg
    assert "loop lift (provisional)" in msg.lower()
    assert "rewrite the check" in msg
    assert "stop lifts remaining" in msg.lower()


# Stop-6 ---------------------------------------------------------------------
def _run_stop(gate_stop, payload):
    captured = {"out": {}}
    gate_stop.read_stdin_json = lambda: payload
    gate_stop.emit_json = lambda d: captured.__setitem__("out", d)
    gate_stop.main()
    return captured["out"]


def _allow_stop_payload(tmp_path):
    return {"session_id": "stop6", "cwd": str(tmp_path)}


def test_stop_tail_emits_empty_when_only_release_verify_pending(monkeypatch, tmp_path):
    import completion_handoff
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "LIGHT")
    monkeypatch.setattr(gate_stop, "warning_after_max_blocks", lambda ledger: "")
    monkeypatch.setattr(gate_stop, "_advance_release", lambda inp: "REL-NOTE-MUST-NOT-LEAK")
    monkeypatch.setattr(gate_stop, "_advance_auto_verify", lambda inp: "VER-NOTE-MUST-NOT-LEAK")
    monkeypatch.setattr(gate_stop, "should_block_stop", lambda ledger, grade: (False, ""))
    monkeypatch.setattr(completion_handoff, "completion_handoff_decision", lambda *a, **k: None)

    out = _run_stop(gate_stop, _allow_stop_payload(tmp_path))
    assert out == {}
    assert "REL-NOTE-MUST-NOT-LEAK" not in str(out)
    assert "VER-NOTE-MUST-NOT-LEAK" not in str(out)


def test_stop_tail_surfaces_only_warning_not_release_verify(monkeypatch, tmp_path):
    import completion_handoff
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "LIGHT")
    monkeypatch.setattr(gate_stop, "warning_after_max_blocks", lambda ledger: "verify-before-finish nudge")
    monkeypatch.setattr(gate_stop, "_advance_release", lambda inp: "REL-NOTE-MUST-NOT-LEAK")
    monkeypatch.setattr(gate_stop, "_advance_auto_verify", lambda inp: "VER-NOTE-MUST-NOT-LEAK")
    monkeypatch.setattr(gate_stop, "should_block_stop", lambda ledger, grade: (False, ""))
    monkeypatch.setattr(completion_handoff, "completion_handoff_decision", lambda *a, **k: None)

    out = _run_stop(gate_stop, _allow_stop_payload(tmp_path))
    assert out.get("systemMessage") == "verify-before-finish nudge"
    assert "REL-NOTE-MUST-NOT-LEAK" not in str(out)
    assert "VER-NOTE-MUST-NOT-LEAK" not in str(out)


# Stop-11 --------------------------------------------------------------------
def test_completion_stop_hint_signal_has_no_duplicated_clause(monkeypatch, tmp_path):
    import gate_stop
    from ledger import save_ledger

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    payload = {"session_id": "hint-sig", "cwd": str(tmp_path)}
    save_ledger(payload, {"completion_stop_blocks": 4})

    captured = {}

    def _capture(spec, **kw):
        captured["signal"] = kw.get("signal", "")
        return ""

    monkeypatch.setattr("spec_judge.judge_hint", _capture)
    spec = {"restated_goal": "test", "tasks": []}
    gate_stop._completion_stop_hint(payload, spec, ["T1", "T2"])
    signal = captured.get("signal", "")
    # The garbled duplicated tail ("...looping on The agent may be looping without
    # converging.") must be gone -- single clause only.
    assert "looping on The agent may be looping" not in signal
    assert signal.count("looping without converging") <= 1


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
