#!/usr/bin/env python3
"""Regression: Stop must always allow when plan mode is enabled.

The evidence gate (gate_stop step 1) is INFINITE and its task checks routinely
require repo mutation that plan mode forbids. Without an explicit plan-mode
allow, Stop blocks forever and the session loops with no way to surface the
plan -- the Codex symptom this fix targets. These tests assert the plan-mode
short-circuit runs before any block path, and that turning plan mode off still
lets the normal evidence gate block.

Run: python3 -m pytest tests/test_stop_plan_mode_allows.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))


def _run_gate_stop_inproc(payload: dict) -> dict:
    import gate_stop

    captured: dict = {}
    gate_stop.read_stdin_json = lambda: payload
    gate_stop.emit_json = lambda data: captured.update({"out": data})
    gate_stop.main()
    return captured.get("out") or {}


@pytest.fixture
def stop_env(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def test_plan_mode_session_context_allows_stop(stop_env, tmp_path):
    # No spec + STANDARD grade would normally block on the evidence gate; the
    # plan-mode flag must short-circuit to an allow ({}) before that.
    payload = {
        "session_id": "sess-plan",
        "cwd": str(tmp_path),
        "stop_hook_active": True,
        "session_context": {"plan_mode_enabled": True},
    }
    out = _run_gate_stop_inproc(payload)
    assert out.get("decision") != "block"


def test_plan_mode_ledger_cache_allows_stop(stop_env, tmp_path):
    from ledger import load_ledger, save_ledger

    payload = {"session_id": "sess-led", "cwd": str(tmp_path)}
    led = load_ledger(payload)
    led["plan_mode_enabled"] = True
    led["plan_mode_host"] = "codex"
    save_ledger(payload, led)

    out = _run_gate_stop_inproc({**payload, "stop_hook_active": True})
    assert out.get("decision") != "block"


def test_plan_mode_off_still_blocks_missing_spec(stop_env, tmp_path):
    # Contrast: with plan mode off and no spec, the evidence gate must still block.
    payload = {"session_id": "sess-noplan", "cwd": str(tmp_path)}
    out = _run_gate_stop_inproc(payload)
    assert out.get("decision") == "block"
