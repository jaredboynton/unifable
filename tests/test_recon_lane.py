#!/usr/bin/env python3
"""recon_lane: host-gated mini exec/recon lane (mini never decides).

Asserts the safety invariants: every command passes the read-only allowlist
before running, mutating commands never run, recon fan-out fails open, and no
code path lets the lane set a task status / emit a verdict.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import recon_lane as rl  # noqa: E402


def test_readonly_command_runs():
    r = rl.run_validation_command("rg --version", ".")
    assert r["allowed"] is True
    assert r["ran"] is True
    assert r["exit_code"] == 0


def test_mutating_command_is_gated_and_never_runs():
    r = rl.run_validation_command("rm -rf /tmp/whatever", ".")
    assert r["allowed"] is False
    assert r["ran"] is False
    assert r["exit_code"] is None
    assert r["reason"]


def test_empty_command_is_inert():
    r = rl.run_validation_command("", ".")
    assert r["ran"] is False and r["allowed"] is False


def test_exit_code_is_deterministic_passthrough():
    ok = rl.run_validation_command("rg -q recon_gather scripts/gate/recon_lane.py", ".")
    bad = rl.run_validation_command("rg -q zzzzz_no_such_token scripts/gate/recon_lane.py", ".")
    assert ok["ran"] and ok["exit_code"] == 0
    assert bad["ran"] and bad["exit_code"] == 1
    # The lane reports the exit code verbatim; it never converts it to a verdict.
    assert "verdict" not in ok and "verdict" not in bad


def test_recon_gather_empty_questions():
    assert rl.recon_gather([], ".") == []


def test_recon_gather_fails_open_without_session(monkeypatch):
    # No bound session + offline -> every slot returns error, never raises.
    monkeypatch.setenv("UNIFABLE_JUDGE_OFFLINE", "1")
    out = rl.recon_gather(["where is the daemon defined?"], ".")
    assert len(out) == 1
    assert out[0].get("error")


def test_recon_gather_coalesces_observations(monkeypatch):
    # mini returns observations only; coalesce is pure formatting, no decision.
    import judge_transport

    def fake_ask(system, user, schema, *, schema_name="result", model=None, **kw):
        assert model == rl.RECON_MODEL
        return {"found": True, "where": "scripts/gate/realtime_daemon.py", "note": "JudgeDaemon class"}

    monkeypatch.setattr(judge_transport, "ask_structured", fake_ask)
    out = rl.recon_gather(["where is the daemon?"], ".")
    assert out[0]["found"] is True
    assert "realtime_daemon" in out[0]["where"]
    blob = rl.coalesce_recon(out)
    assert "found @ scripts/gate/realtime_daemon.py" in blob
    # No status / verdict leaks into the coalesced evidence.
    assert "validated" not in blob and "verdict" not in blob


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
