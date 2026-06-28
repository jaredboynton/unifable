#!/usr/bin/env python3
"""Tests for the actionable SessionStart frame.

The standing block no longer front-loads the model with operating-mode posture.
It tells the agent exactly which CLI command to run first, then gives only the
initial research-mode restrictions.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parent.parent / "scripts" / "gate"
if str(GATE_DIR) not in sys.path:
    sys.path.insert(0, str(GATE_DIR))

import context_block  # noqa: E402
from research_bash_guidance import bash_allowed_summary  # noqa: E402


def test_frame_nonempty_and_deterministic() -> None:
    ctx1 = context_block.build_session_context()
    ctx2 = context_block.build_session_context()
    assert isinstance(ctx1, str)
    assert len(ctx1.strip()) > 0
    assert ctx1 == ctx2


def test_frame_instructs_exact_restate_first() -> None:
    ctx = context_block.build_session_context()
    assert len(ctx.splitlines()) >= 2


def test_frame_names_one_first_action_and_then_defers_to_hooks() -> None:
    """SessionStart should give one actionable next step, then defer follow-ups."""
    ctx = context_block.build_session_context()
    assert len([line for line in ctx.splitlines() if line.strip()]) < 12


def test_frame_has_compact_imperative_structure() -> None:
    """The frame stays focused on next action, preflight limits, and hook follow-up."""
    ctx = context_block.build_session_context()
    lines = [line for line in ctx.splitlines() if line.strip()]
    assert len(lines[1].split()) >= 2


def test_frame_carries_preflight_guidance() -> None:
    """The frame names the exact tools allowed during initial research mode."""
    ctx = context_block.build_session_context()
    assert f"{bash_allowed_summary()}." in ctx


def test_frame_is_thin() -> None:
    ctx = context_block.build_session_context()
    assert len(ctx) < 900, f"frame is not thin: {len(ctx)} chars"


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
