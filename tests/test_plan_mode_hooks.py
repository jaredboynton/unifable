#!/usr/bin/env python3
"""Plan mode messaging on UserPromptSubmit and PreToolUse hooks."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "gate"
HOOKS = REPO / "hooks"
sys.path.insert(0, str(GATE))
sys.path.insert(0, str(HOOKS))

import gate_prompt  # noqa: E402
import pre_tool_use  # noqa: E402
from ledger import load_ledger, update_ledger  # noqa: E402
from plan_mode import append_plan_mode_note  # noqa: E402


def _run_gate_prompt(payload: dict, monkeypatch) -> dict:
    captured: dict = {}

    def fake_judge(*_a, **_k):
        return {"mode": "normal", "risks": [], "reason": "ok", "evidence_profile": "code"}

    monkeypatch.setattr(gate_prompt, "read_stdin_json", lambda: payload)
    monkeypatch.setattr(gate_prompt, "emit_json", lambda d: captured.update({"out": d}))
    monkeypatch.setattr(gate_prompt, "judge_grade_classify", fake_judge)
    monkeypatch.setattr(gate_prompt, "parse_grade_verdict", lambda v: ("normal", [], "ok", "code"))
    gate_prompt.main()
    return captured.get("out") or {}


def test_gate_prompt_injects_plan_mode_context(tmp_path, monkeypatch):
    os.environ["UNIFABLE_DATA"] = str(tmp_path)
    prompt = "plan the feature\n\n<system_reminder>Plan mode is active. Do not execute.</system_reminder>"
    payload = {
        "prompt": prompt,
        "cwd": str(tmp_path),
        "session_id": "plan-prompt-test",
    }
    out = _run_gate_prompt(payload, monkeypatch)
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "Plan Mode:" in ctx
    assert "repo-tracked writes forbidden" in ctx.lower()


def test_gate_prompt_sets_ledger_plan_mode(tmp_path, monkeypatch):
    os.environ["UNIFABLE_DATA"] = str(tmp_path)
    prompt = "<system_reminder>Plan mode is active</system_reminder>"
    payload = {"prompt": prompt, "cwd": str(tmp_path), "session_id": "plan-ledger-test"}
    _run_gate_prompt(payload, monkeypatch)
    ledger = load_ledger(payload)
    assert ledger.get("plan_mode_enabled") is True
    assert ledger.get("plan_mode_host") == "cursor"


def test_pretool_block_appends_plan_mode_note(tmp_path, monkeypatch, capsys):
    os.environ["UNIFABLE_DATA"] = str(tmp_path)
    payload = {"session_id": "pretool-plan", "cwd": str(tmp_path)}

    def apply(ledger):
        ledger["plan_mode_enabled"] = True
        ledger["plan_mode_host"] = "cursor"

    update_ledger(payload, apply)
    # No mark_plan_mode_prompt_notified — plan note should appear on block.

    rc = pre_tool_use._block(
        payload,
        kind="spec",
        detail="missing",
        message="no evidence spec",
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "Plan Mode:" in err


def test_pretool_block_no_note_when_agent_mode(tmp_path, monkeypatch, capsys):
    os.environ["UNIFABLE_DATA"] = str(tmp_path)
    payload = {"session_id": "pretool-agent", "cwd": str(tmp_path)}

    def apply(ledger):
        ledger["plan_mode_enabled"] = False
        ledger["plan_mode_host"] = ""

    update_ledger(payload, apply)

    pre_tool_use._block(payload, kind="spec", detail="missing", message="no evidence spec")
    err = capsys.readouterr().err
    assert "Plan Mode:" not in err


def test_emit_pretool_with_plan_note_direct():
    msg = append_plan_mode_note("blocked", {"enabled": True, "host": "codex"})
    assert "Plan Mode:" in msg
