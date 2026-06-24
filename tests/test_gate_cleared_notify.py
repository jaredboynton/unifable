#!/usr/bin/env python3
"""Gate cleared notification on PreToolUse allow after prior block."""

from __future__ import annotations

import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

from pretool_block import consume_gate_cleared_notify  # noqa: E402


def test_consume_gate_cleared_after_recorded_block(tmp_path, monkeypatch):
    recorded = {}

    def fake_update(input_data, fn):
        recorded.update({"session_id": input_data.get("session_id"), "fn": fn})

    def fake_load(input_data):
        return {
            "pretool_last_block_kind": "spec",
            "pretool_last_block_detail": "citations:repo_context[0]",
        }

    import ledger

    monkeypatch.setattr(ledger, "load_ledger", fake_load)
    monkeypatch.setattr(ledger, "update_ledger", fake_update)

    msg = consume_gate_cleared_notify(
        {"session_id": "s1"},
        ["Removed invalid auto-sync citation(s) (path does not exist): x."],
    )
    assert msg.startswith("Gate cleared.")
    assert "Removed invalid auto-sync" in msg
    assert recorded.get("session_id") == "s1"


def test_consume_gate_cleared_empty_without_prior_block(monkeypatch):
    import ledger

    monkeypatch.setattr(ledger, "load_ledger", lambda _inp: {})
    monkeypatch.setattr(ledger, "update_ledger", lambda *_a, **_k: None)
    assert consume_gate_cleared_notify({"session_id": "s1"}, []) == ""
