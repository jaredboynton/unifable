#!/usr/bin/env python3
"""Host detection tests for hook output shaping."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parent.parent / "scripts" / "gate"
if str(GATE_DIR) not in sys.path:
    sys.path.insert(0, str(GATE_DIR))

import hook_output  # noqa: E402


def _reload(monkeypatch):
    for key in (
        "UNIFABLE_HOST",
        "PLUGIN_ROOT",
        "CLAUDE_PLUGIN_ROOT",
        "UNIFABLE_PLUGIN_ROOT",
        "CODEX_THREAD_ID",
        "CLAUDE_CODE_SESSION_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    return importlib.reload(hook_output)


def test_forced_host_wins(monkeypatch):
    mod = _reload(monkeypatch)
    monkeypatch.setenv("UNIFABLE_HOST", "claude")
    assert mod.detect_host({"turn_id": "codex-looking"}) == "claude"


def test_codex_thread_id_wins_over_claude_session(monkeypatch):
    mod = _reload(monkeypatch)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude")
    assert mod.detect_host({}) == "codex"


def test_claude_session_wins_over_turn_id_when_no_codex_thread(monkeypatch):
    mod = _reload(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude")
    assert mod.detect_host({"turn_id": "future-claude-turn"}) == "claude"


def test_plugin_root_detects_codex_and_claude(monkeypatch):
    mod = _reload(monkeypatch)
    monkeypatch.setenv("PLUGIN_ROOT", "/tmp/.codex/plugins/unifable")
    assert mod.detect_host({}) == "codex"

    mod = _reload(monkeypatch)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/tmp/.claude/plugins/unifable")
    assert mod.detect_host({}) == "claude"
