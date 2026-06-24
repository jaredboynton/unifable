#!/usr/bin/env python3
"""Hook token dedup: scaffold once, plan mode cache, pretool footer, posttool partials."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import gate_prompt  # noqa: E402
import pre_tool_use  # noqa: E402
from ledger import load_ledger, update_ledger  # noqa: E402
from model_notify import build_stop_validate_context  # noqa: E402
from plan_mode import mark_plan_mode_prompt_notified  # noqa: E402
from posttool_notify import filter_breaker_status, prepare_posttool_parts  # noqa: E402
from pretool_block import format_bash_research_block, format_delegation_block  # noqa: E402
from spec import format_spec_validation_block, spec_template  # noqa: E402


def _run_prompt(payload: dict, monkeypatch) -> str:
    import cli_install

    monkeypatch.setattr(cli_install, "ensure_cli", lambda: None)
    captured: dict = {}

    def fake_judge(*_a, **_k):
        return {"mode": "normal", "risk_flags": [], "reason": "ok", "evidence_profile": "code"}

    monkeypatch.setattr(gate_prompt, "read_stdin_json", lambda: payload)
    monkeypatch.setattr(gate_prompt, "emit_json", lambda d: captured.update({"out": d}))
    monkeypatch.setattr(gate_prompt, "judge_grade_classify", fake_judge)
    monkeypatch.setattr(gate_prompt, "parse_grade_verdict", lambda v: ("normal", [], "ok", "code"))
    gate_prompt.main()
    return (captured.get("out", {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""


def test_scaffold_tutorial_once_per_session(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_GRADE", raising=False)
    base = {"session_id": "scaffold-once", "cwd": str(tmp_path)}
    ctx1 = _run_prompt({**base, "prompt": "first task in session"}, monkeypatch)
    assert "evidence spec auto-created" in ctx1
    assert "unifable restate" in ctx1
    ctx2 = _run_prompt({**base, "prompt": "second message same session"}, monkeypatch)
    assert "evidence spec auto-created" not in ctx2
    assert "unifable restate" not in ctx2 or "scaffold updated" in ctx2


def test_plan_mode_pretool_skips_note_after_prompt(tmp_path, capsys):
    os.environ["UNIFABLE_DATA"] = str(tmp_path)
    payload = {"session_id": "plan-skip", "cwd": str(tmp_path), "turn_id": "t1"}

    def apply(ledger):
        ledger["plan_mode_enabled"] = True
        ledger["plan_mode_host"] = "cursor"

    update_ledger(payload, apply)
    mark_plan_mode_prompt_notified(payload)

    pre_tool_use._block(payload, kind="spec", detail="missing", message="no evidence spec")
    err = capsys.readouterr().err
    assert "Plan Mode active" not in err


def test_stop_reason_omits_hints_when_validate_ctx_present():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    spec["tasks"] = [
        {
            "id": "T1",
            "title": "x",
            "check": "true",
            "status": "failed",
            "judge_reason": "needs proof",
            "judge_hint": "run the test",
        },
    ]
    headlines = ["T1 check ran (exit 1); judge rejected the evidence."]
    ctx, _ = build_stop_validate_context(spec, headlines)
    assert "needs proof" in ctx
    reason = "breaker CLOSED: 1 task(s) not validated (T1)."
    assert "Action:" not in reason
    assert "needs proof" not in reason


def test_pretool_blocks_share_unlock_footer_wording():
    bash = format_bash_research_block("nl blocked", "s1")
    delegate = format_delegation_block("Task", "s1")
    assert "Unlock: unifable restate" in bash
    assert "Unlock: unifable restate" in delegate


def test_format_spec_validation_block_compact_multiline():
    reasons = ["missing repo_context", "missing prior_art"]
    msg = format_spec_validation_block("STANDARD", reasons, include_contract=False)
    assert msg.startswith("Evidence spec does not satisfy grade STANDARD:")
    assert "  missing repo_context" in msg
    assert "  missing prior_art" in msg
    assert msg.count("To unblock edits:") == 1
    assert "; missing prior_art" not in msg


def test_posttool_breaker_status_deduped():
    ledger = {"posttool_last_breaker_status": "breaker: ARMED on 'claim'"}
    assert filter_breaker_status(ledger, "breaker: ARMED on 'claim'") == ""
    assert filter_breaker_status(ledger, "breaker: ARMED on 'other'") != ""


def test_posttool_prepare_strips_repeat_breaker_line():
    payload = {"session_id": "pt", "cwd": "/tmp", "turn_id": "t1"}
    parts = ["breaker: ARMED on 'x'", "synced 1 cite(s): repo_context<-read [a.py:1]"]
    out1, _ = prepare_posttool_parts(payload, parts)
    assert any(p.startswith("breaker:") for p in out1)
    update_ledger(payload, lambda ld: ld.update({"posttool_last_breaker_status": "breaker: ARMED on 'x'"}))
    out2, _ = prepare_posttool_parts(payload, parts)
    assert not any(p.startswith("breaker:") for p in out2)
