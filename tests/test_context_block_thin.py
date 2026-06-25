#!/usr/bin/env python3
"""Tests for the THIN SessionStart frame (stepwise judge-driven harness).

The standing block no longer front-loads the model with the full operating-mode
posture. It only frames the judge relationship and tells the agent to restate the
goal; the per-tool director judge supplies step-by-step guidance at runtime.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parent.parent / "scripts" / "gate"
if str(GATE_DIR) not in sys.path:
    sys.path.insert(0, str(GATE_DIR))

import context_block  # noqa: E402


def test_frame_nonempty_and_deterministic() -> None:
    ctx1 = context_block.build_session_context()
    ctx2 = context_block.build_session_context()
    assert isinstance(ctx1, str)
    assert len(ctx1.strip()) > 0
    assert ctx1 == ctx2


def test_frame_states_judge_relationship() -> None:
    ctx = context_block.build_session_context().lower()
    # Names the judge and its step-by-step, tool-gating role.
    assert "judge" in ctx
    assert "step" in ctx
    # The judge tends the spec on the agent's behalf.
    assert "task" in ctx


def test_frame_instructs_restate_first() -> None:
    ctx = context_block.build_session_context()
    assert "unifable restate" in ctx


def test_frame_drops_the_old_standing_posture() -> None:
    """The fat operating-mode fragments must be gone -- the director carries them
    per step now, so they must not be front-loaded here."""
    ctx = context_block.build_session_context()
    for gone in (
        "malformed compounds",
        "rg/grep/ast-grep",
        "Lead with the outcome",
        "Orchestrator posture",
        "head/wc/tail not cat",
        "Read before naming",
    ):
        assert gone not in ctx, f"thin frame still carries removed fragment: {gone!r}"


def test_frame_is_thin() -> None:
    ctx = context_block.build_session_context()
    # Far below the old ~3KB block. The thin frame is a short paragraph.
    assert len(ctx) < 1200, f"frame is not thin: {len(ctx)} chars"


def test_payload_shape() -> None:
    payload = context_block.build_session_payload()
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert isinstance(hso["additionalContext"], str)
    assert len(hso["additionalContext"]) > 0


def test_payload_fails_open_on_error() -> None:
    importlib.reload(context_block)
    payload = context_block.build_session_payload()
    assert "hookSpecificOutput" in payload


def test_no_emoji_in_frame() -> None:
    ctx = context_block.build_session_context()
    for ch in ctx:
        assert ord(ch) < 0x1F000 or ch and ord(ch) > 0x1FAFF, f"unexpected emoji: {ch!r}"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
