#!/usr/bin/env python3
"""Fix B: the evidence-spec board (validate_ctx, e.g. "Spec complete: all tasks
validated.") must ride ONLY the step-1 evidence-gate block, never a
completion-handoff block. Otherwise a single Stop emits a self-contradictory
"blocked + all tasks validated" message (the reported contention)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import completion_handoff  # noqa: E402
import gate_stop  # noqa: E402
import spec_stop_validate  # noqa: E402

_BOARD = (
    "=== EVIDENCE SPEC BOARD (authoritative task status) ===\n"
    "Spec complete: all tasks validated.\n"
    "=== END EVIDENCE SPEC BOARD ==="
)


def _write_transcript(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "All tasks validated. Want me to commit?"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _save_validated_spec(cwd: str, session_id: str) -> None:
    from spec_io import save_spec

    save_spec(
        cwd,
        session_id,
        {
            "restated_goal": "fix the gate bugs, restated in my own words",
            # operational profile waives repo_context/prior_art so step 1's evidence
            # sub-check passes and execution reaches the step-3 handoff branch (the
            # path under test). spec > ledger > code precedence (evidence_policy).
            "evidence_profile": "operational",
            "acceptance_criteria": [{"check": "true", "evidence": "ran -> ok"}],
            "tasks": [{"id": "T1", "title": "t", "check": "true", "status": "validated"}],
        },
    )


def test_handoff_block_does_not_carry_evidence_board(tmp_path, monkeypatch):
    cwd = tmp_path
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript)
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    _save_validated_spec(str(cwd), "sess_board")

    # Step 1: avoid real judge calls; inject a board headline so the step-1
    # validate_ctx local is non-empty (this is what used to leak onto the block).
    monkeypatch.setattr(
        spec_stop_validate,
        "auto_validate_spec",
        lambda spec, _cwd, **_k: (spec, ["Spec complete: all tasks validated."]),
    )
    monkeypatch.setattr(
        gate_stop,
        "_build_stop_validate_context",
        lambda spec, val_msgs, **_k: ((_BOARD, False) if val_msgs else ("", False)),
    )

    # Step 3: force a handoff block (bypassing Fix A) so Fix B is exercised in isolation.
    monkeypatch.setattr(
        completion_handoff,
        "completion_handoff_decision",
        lambda input_data, cwd: {
            "decision": "block",
            "reason": "Stop blocked: finish the pending work now.",
            "_handoff_steering": "do it",
        },
    )

    captured: dict = {}

    def _capture(payload, input_data, **kwargs):
        captured["payload"] = payload
        captured["validate_ctx"] = kwargs.get("validate_ctx", "<missing>")

    monkeypatch.setattr(gate_stop, "_emit_stop_payload", _capture)
    monkeypatch.setattr(
        gate_stop,
        "read_stdin_json",
        lambda: {
            "session_id": "sess_board",
            "cwd": str(cwd),
            "transcript_path": str(transcript),
            "stop_hook_active": True,
        },
    )

    gate_stop.main()

    assert captured.get("payload", {}).get("decision") == "block"
    assert "Stop blocked: finish the pending work now." in captured["payload"].get("reason", "")
    assert captured["validate_ctx"] == ""
