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
    assert ctx.startswith("FIRST ACTION REQUIRED")
    assert "first tool call MUST run this CLI command" in ctx
    assert "unifable restate '<goal in your own words>'" in ctx
    assert "Do it RIGHT NOW" in ctx
    assert "restat" in ctx.lower()


def test_frame_does_not_duplicate_full_spec_cli_tutorial() -> None:
    """SessionStart names only the mandatory first command, not the full CLI."""
    ctx = context_block.build_session_context()
    assert "unifable add-task" not in ctx
    assert "unifable set-primary" not in ctx
    assert "unifable add-frontier" not in ctx


def test_frame_drops_the_old_standing_posture() -> None:
    """The frame must stay imperative and avoid relationship/posture prose."""
    ctx = context_block.build_session_context()
    for gone in (
        "Stepwise, judge-driven operating mode",
        "judge agent",
        "tends a goal spec",
        "groundedness arm",
        "malformed compounds",
        "rg/grep/ast-grep",
        "Lead with the outcome",
        "Orchestrator posture",
        "head/wc/tail not cat",
        "Read before naming",
    ):
        assert gone not in ctx, f"startup frame still carries removed fragment: {gone!r}"


def test_frame_carries_preflight_guidance() -> None:
    """The frame names the exact tools allowed during initial research mode."""
    ctx = context_block.build_session_context()
    assert "Inspection tools stay available: Read, Grep, Glob, WebSearch, WebFetch, NotebookRead." in ctx
    assert f"Bash/REPL/exec_command are limited to: {bash_allowed_summary()}." in ctx
    assert "Write tools (Edit, Write, MultiEdit, NotebookEdit, apply_patch) and delegation stay blocked" in ctx
    assert "Research mode allows only Read, Grep, Glob, and python3 -c" not in ctx


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
