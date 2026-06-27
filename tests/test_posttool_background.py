#!/usr/bin/env python3
"""Fire-and-forget background reconcile/discover: lease debounce, pending queue,
and the PreToolUse drain that injects the completed context one tool-step later."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import db  # noqa: E402
import posttool_background  # noqa: E402

# --- db lease + pending queue ----------------------------------------------


def test_bg_lease_debounces_within_ttl():
    with tempfile.TemporaryDirectory() as data_dir:
        os.environ["UNIFABLE_DATA"] = data_dir
        now = time.time()
        assert db.posttool_bg_lease("k", now, 90.0) is True
        # Second caller within the TTL window must not spawn.
        assert db.posttool_bg_lease("k", now + 1.0, 90.0) is False
        # After the window, a new job may lease again.
        assert db.posttool_bg_lease("k", now + 200.0, 90.0) is True


def test_bg_push_then_drain_round_trips_and_clears():
    with tempfile.TemporaryDirectory() as data_dir:
        os.environ["UNIFABLE_DATA"] = data_dir
        db.posttool_bg_push("k", "Spec update:\nT3 revised: x")
        assert db.posttool_bg_drain("k") == "Spec update:\nT3 revised: x"
        # Drained once -> empty thereafter.
        assert db.posttool_bg_drain("k") == ""


def test_bg_push_appends_undrained_blocks():
    with tempfile.TemporaryDirectory() as data_dir:
        os.environ["UNIFABLE_DATA"] = data_dir
        db.posttool_bg_push("k", "first")
        db.posttool_bg_push("k", "second")
        drained = db.posttool_bg_drain("k")
        assert "first" in drained and "second" in drained


def test_bg_push_clears_lease():
    with tempfile.TemporaryDirectory() as data_dir:
        os.environ["UNIFABLE_DATA"] = data_dir
        now = time.time()
        assert db.posttool_bg_lease("k", now, 90.0) is True
        assert db.posttool_bg_lease("k", now + 1.0, 90.0) is False  # in-flight
        db.posttool_bg_push("k", "done")  # job finished -> lease released
        assert db.posttool_bg_lease("k", now + 2.0, 90.0) is True


# --- spawn gating -----------------------------------------------------------


def test_spawn_disabled_by_env(monkeypatch):
    monkeypatch.setenv("UNIFABLE_POSTTOOL_BG", "0")
    assert posttool_background.spawn_reconcile_job({}, want_reconcile=True, want_discover=True) is False


def test_spawn_noop_when_nothing_requested(monkeypatch):
    monkeypatch.setenv("UNIFABLE_POSTTOOL_BG", "1")
    assert posttool_background.spawn_reconcile_job({}, want_reconcile=False, want_discover=False) is False


# --- PreToolUse drain -------------------------------------------------------


def test_pretool_drains_pending_bg_context(monkeypatch):
    import pre_tool_use

    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        payload = {"session_id": "sess", "cwd": cwd}
        spec_key = posttool_background._spec_key_for(payload)
        db.posttool_bg_push(spec_key, "Spec update:\nJudge retracted T1: gone")

        # _drain_bg_context surfaces the completed context exactly once.
        first = pre_tool_use._drain_bg_context(payload)
        assert "Judge retracted T1: gone" in first
        assert pre_tool_use._drain_bg_context(payload) == ""
