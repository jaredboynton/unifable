#!/usr/bin/env python3
"""Regression: gate_stop must never emit hookSpecificOutput on Codex Stop hooks.

Codex rejects Stop stdout that includes hookSpecificOutput (valid JSON, wrong shape).
Mirror codex-rs output_parser: Stop expects top-level decision/reason/systemMessage only.
Run: python3 -m pytest tests/test_stop_codex_json.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import spec as spec_mod  # noqa: E402
from spec import save_spec, spec_template  # noqa: E402


def codex_stop_verdict(stdout: str) -> str:
    """Mirror Codex Stop hook JSON acceptance."""
    trimmed = stdout.strip()
    if not trimmed:
        return "FAIL_EMPTY"
    try:
        value = json.loads(trimmed)
    except json.JSONDecodeError:
        return "FAIL_PARSE"
    if not isinstance(value, dict):
        return "FAIL"
    if "hookSpecificOutput" in value:
        return "FAIL"
    return "OK"


def _task(tid: str, status: str, **extra):
    t = {"id": tid, "title": tid, "check": "true", "status": status}
    t.update(extra)
    return t


def _run_gate_stop(payload: dict, *, monkeypatch) -> str:
    import gate_stop

    captured: dict = {}

    def _capture(data: dict) -> None:
        captured["out"] = data

    gate_stop.read_stdin_json = lambda: payload
    gate_stop.emit_json = _capture
    gate_stop.main()
    return json.dumps(captured.get("out") or {})


@pytest.fixture
def stop_env(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")


def test_codex_stop_block_without_hook_specific_output(stop_env, tmp_path, monkeypatch):
    import gate_stop  # noqa: F401

    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(
        spec_mod,
        "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(0, "T1 needs more proof", [], "") for _ in items],
    )

    payload = {"session_id": "sess", "cwd": str(tmp_path), "turn_id": "codex-turn-1"}
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    stdout = _run_gate_stop(payload, monkeypatch=monkeypatch)
    assert codex_stop_verdict(stdout) == "OK"
    out = json.loads(stdout)
    assert out.get("decision") == "block"
    assert "T1 needs more proof" in (out.get("reason") or "")
    assert "hookSpecificOutput" not in out


def test_claude_stop_block_keeps_hook_specific_output(stop_env, tmp_path, monkeypatch):
    import gate_stop  # noqa: F401

    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(
        spec_mod,
        "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(0, "T1 needs more proof", [], "") for _ in items],
    )
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-sess")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("PLUGIN_ROOT", raising=False)

    payload = {"session_id": "sess", "cwd": str(tmp_path)}
    stdout = _run_gate_stop(payload, monkeypatch=monkeypatch)
    out = json.loads(stdout)
    assert out.get("decision") == "block"
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "T1 needs more proof" in ctx


def test_hook_output_finalize_codex_unit():
    from hook_output import finalize_stop_payload

    payload = {"decision": "block", "reason": "breaker CLOSED"}
    out = finalize_stop_payload(
        payload,
        validate_ctx="unifable spec update (stop validation):\nT1: do work",
        host="codex",
    )
    assert "hookSpecificOutput" not in out
    assert "T1: do work" in out["reason"]
    assert "breaker CLOSED" in out["reason"]
