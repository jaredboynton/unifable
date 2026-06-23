#!/usr/bin/env python3
"""Integration test: gate_prompt.py calls the judge classifier and sets the ledger.

Drives the UserPromptSubmit hook with a fake judge, asserting the ledger gets
the correct task_mode / grade / risk_flags for each classification verdict,
including the fail-open path when the judge is unreachable.
Run: python3 -m pytest tests/test_grade_classify_integration.py -q
"""
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

from ledger import load_ledger  # noqa: E402


def _run_gate_prompt(payload: dict, judge_verdict: dict | None):
    import gate_prompt

    def fake_judge(operative, **kw):
        if isinstance(judge_verdict, Exception):
            raise judge_verdict
        return judge_verdict

    with patch("gate_prompt.judge_grade_classify", side_effect=fake_judge):
        with patch("gate_prompt.read_stdin_json", return_value=payload):
            with patch.object(gate_prompt, "emit_json") as emit:
                rc = gate_prompt.main()
                out = emit.call_args[0][0] if emit.call_count else {}
    return rc, out


def _payload(cwd, prompt="fix the auth bug in gate_prompt.py", session="test-classify"):
    return {"prompt": prompt, "session_id": session, "cwd": cwd}


def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_BREAKER", "0")
    return str(tmp_path)


def test_normal_classification_sets_standard_grade(monkeypatch, tmp_path):
    cwd = _setup(monkeypatch, tmp_path)
    verdict = {"mode": "normal", "risk_flags": [], "reason": "bounded fix"}
    rc, _ = _run_gate_prompt(_payload(cwd), verdict)
    assert rc == 0
    led = load_ledger({"session_id": "test-classify", "cwd": cwd})
    assert led["task_mode"] == "normal"
    assert led["grade"] == "STANDARD"


def test_deep_classification_sets_heavy_grade(monkeypatch, tmp_path):
    cwd = _setup(monkeypatch, tmp_path)
    verdict = {"mode": "deep", "risk_flags": ["architectural"], "reason": "migration"}
    rc, _ = _run_gate_prompt(_payload(cwd, "migrate to event-driven"), verdict)
    assert rc == 0
    led = load_ledger({"session_id": "test-classify", "cwd": cwd})
    assert led["task_mode"] == "deep"
    assert led["grade"] == "HEAVY"


def test_quick_classification_sets_light_grade(monkeypatch, tmp_path):
    cwd = _setup(monkeypatch, tmp_path)
    verdict = {"mode": "quick", "risk_flags": [], "reason": "explain only"}
    rc, _ = _run_gate_prompt(_payload(cwd, "explain how this works"), verdict)
    assert rc == 0
    led = load_ledger({"session_id": "test-classify", "cwd": cwd})
    assert led["task_mode"] == "quick"
    assert led["grade"] == "LIGHT"


def test_uncertainty_risk_flag_preserved(monkeypatch, tmp_path):
    cwd = _setup(monkeypatch, tmp_path)
    verdict = {"mode": "normal", "risk_flags": ["uncertainty"], "reason": "hedged"}
    rc, _ = _run_gate_prompt(_payload(cwd, "maybe it's a cache thing?"), verdict)
    assert rc == 0
    led = load_ledger({"session_id": "test-classify", "cwd": cwd})
    assert "uncertainty" in led.get("risk_flags", [])


def test_fail_open_on_judge_unreachable(monkeypatch, tmp_path):
    """When the judge returns None (transport failure), the hook must not crash
    and the ledger must fall back to normal/STANDARD."""
    cwd = _setup(monkeypatch, tmp_path)
    rc, _ = _run_gate_prompt(_payload(cwd), None)
    assert rc == 0
    led = load_ledger({"session_id": "test-classify", "cwd": cwd})
    assert led["task_mode"] == "normal"
    assert led["grade"] == "STANDARD"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
