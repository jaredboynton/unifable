#!/usr/bin/env python3
"""Smoke tests for the SessionStart hook's janitor dispatch.

Confirms the hook still emits the unchanged SessionStart payload and exits 0,
and that it writes this session's alive-marker (so the reaper can never clean a
live session) while NOT spawning the reaper when UNIFABLE_JANITOR=0.

Run: python3 -m pytest tests/test_session_start_janitor.py -q
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = str(REPO / "scripts" / "gate")
HOOKS = str(REPO / "hooks")

sys.path.insert(0, GATE)

import ledger  # noqa: E402


def _load_session_start():
    sys.path.insert(0, HOOKS)
    return importlib.import_module("session_start")


def test_dispatch_writes_alive_marker_without_spawning(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_JANITOR", "0")
    repo = str(tmp_path / "repo")
    monkeypatch.setenv("UNIFABLE_PROJECT_ROOT", repo)
    input_data = {"session_id": "sid-123", "cwd": repo}
    expected_skey = ledger.ledger_key(input_data)

    session_start = _load_session_start()
    session_start._dispatch_janitor(input_data, HOOKS)

    marker_path = tmp_path / "alive" / f"{expected_skey}.json"
    assert marker_path.is_file(), "alive-marker must be written for this session"
    marker = json.loads(marker_path.read_text())
    assert marker["skey"] == expected_skey
    assert marker["session_id"] == "sid-123"
    assert marker["project_root"] == os.path.realpath(repo)
    assert "host_pid" in marker and "host_comm" in marker
    # Disabled janitor -> no sweep sentinel touched.
    assert not (tmp_path / "alive" / ".last_sweep").exists()


def test_main_emits_sessionstart_payload_and_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_JANITOR", "0")
    repo = str(tmp_path / "repo")
    monkeypatch.setenv("UNIFABLE_PROJECT_ROOT", repo)
    payload_in = {"session_id": "sid-xyz", "cwd": repo}

    # Stub runtime_sync so main() does not copy the runtime tree.
    monkeypatch.setitem(sys.modules, "runtime_sync", types.SimpleNamespace(sync_runtime=lambda: None))

    session_start = _load_session_start()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload_in)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    rc = session_start.main()

    assert rc == 0
    emitted = json.loads(out.getvalue())
    assert "hookSpecificOutput" in emitted
    assert emitted["hookSpecificOutput"].get("hookEventName") == "SessionStart"
    # And the alive-marker was still written for this session.
    skey = ledger.ledger_key(payload_in)
    assert (tmp_path / "alive" / f"{skey}.json").is_file()
    assert not (tmp_path / "alive" / ".last_sweep").exists()


def test_main_with_no_stdin_still_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_JANITOR", "0")
    monkeypatch.setitem(sys.modules, "runtime_sync", types.SimpleNamespace(sync_runtime=lambda: None))

    # A TTY-like stdin (isatty True) -> no payload, must not block or crash.
    class TtyStdin(io.StringIO):
        def isatty(self):
            return True

    session_start = _load_session_start()
    monkeypatch.setattr(sys, "stdin", TtyStdin(""))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert session_start.main() == 0
    json.loads(out.getvalue())  # valid JSON emitted
