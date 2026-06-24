#!/usr/bin/env python3
"""Tests for scripts/gate/context_block.py — the SessionStart context that
replaced the old static CLAUDE.md/AGENTS.md block injection.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parent.parent / "scripts" / "gate"
if str(GATE_DIR) not in sys.path:
    sys.path.insert(0, str(GATE_DIR))

import context_block  # noqa: E402


def test_build_session_context_nonempty() -> None:
    ctx = context_block.build_session_context()
    assert isinstance(ctx, str)
    assert len(ctx.strip()) > 0


def test_build_session_context_contains_key_sections() -> None:
    ctx = context_block.build_session_context()
    # Citation rule (standing posture)
    assert "path:line" in ctx
    assert "(assumption)" in ctx
    # Evidence gate pointer
    assert "evidence gate" in ctx.lower() or "evidence spec" in ctx.lower()
    assert "unifable restate" in ctx
    assert "unifable add-task" in ctx
    # Hook-block guidance
    assert "hook" in ctx.lower()
    # Orchestrator posture
    assert "orchestrat" in ctx.lower()
    # Edit discipline
    assert "malformed compounds" in ctx
    # Final response shape
    assert "Lead with the outcome" in ctx


def test_build_session_context_under_budget() -> None:
    ctx = context_block.build_session_context()
    # Must be well under the old ~3KB static block. 4KB ceiling gives room for
    # explore-skill path expansion.
    assert len(ctx) < 4000


def test_build_session_context_deterministic() -> None:
    ctx1 = context_block.build_session_context()
    ctx2 = context_block.build_session_context()
    assert ctx1 == ctx2


def test_build_session_payload_shape() -> None:
    payload = context_block.build_session_payload()
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert isinstance(payload["hookSpecificOutput"]["additionalContext"], str)
    assert len(payload["hookSpecificOutput"]["additionalContext"]) > 0


def test_build_session_payload_fails_open_on_error() -> None:
    """A research_bash_guidance import failure must not crash the payload builder."""
    importlib.reload(context_block)
    # The module already handles missing research_bash_guidance via fallback stubs.
    payload = context_block.build_session_payload()
    assert "hookSpecificOutput" in payload


def test_no_emoji_in_context() -> None:
    ctx = context_block.build_session_context()
    # No checkmark or emoji codepoints
    for ch in ctx:
        assert ord(ch) < 0x1F000 or ord(ch) > 0x1FAFF, f"unexpected emoji: {ch!r}"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
