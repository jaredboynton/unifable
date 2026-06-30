#!/usr/bin/env python3
"""realtime_daemon version-stamped self-exit: when runtime_sync flips
~/.unifable/current to a new version, an already-running daemon drains and exits
so the next connectOrSpawn relaunches it on fresh code. Fail-open: an
unresolvable version is treated as 'no change'.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import realtime_daemon as rd  # noqa: E402


def _daemon():
    d = rd.JudgeDaemon(session_key="k", sock_path="/tmp/uni-selfupdate-test.sock", pool_size=1)
    d._last_version_check = 0.0  # bypass the throttle window
    return d


def test_version_change_triggers_self_exit(monkeypatch):
    monkeypatch.setattr(rd, "SELF_UPDATE", True)
    monkeypatch.setattr(rd, "_runtime_version", lambda: "1.21.8")
    d = _daemon()
    d._boot_version = "1.21.7"
    assert d._stop.is_set() is False
    d._maybe_self_update_exit()
    assert d._stop.is_set() is True, "a flipped current must drain+exit the daemon"


def test_same_version_does_not_exit(monkeypatch):
    monkeypatch.setattr(rd, "SELF_UPDATE", True)
    monkeypatch.setattr(rd, "_runtime_version", lambda: "1.21.7")
    d = _daemon()
    d._boot_version = "1.21.7"
    d._maybe_self_update_exit()
    assert d._stop.is_set() is False


def test_unresolvable_version_is_no_change(monkeypatch):
    monkeypatch.setattr(rd, "SELF_UPDATE", True)
    monkeypatch.setattr(rd, "_runtime_version", lambda: None)
    d = _daemon()
    d._boot_version = "1.21.7"
    d._maybe_self_update_exit()
    assert d._stop.is_set() is False, "a transient resolve failure must not self-exit"


def test_disabled_flag_skips_check(monkeypatch):
    monkeypatch.setattr(rd, "SELF_UPDATE", False)
    monkeypatch.setattr(rd, "_runtime_version", lambda: "9.9.9")
    d = _daemon()
    d._boot_version = "1.21.7"
    d._maybe_self_update_exit()
    assert d._stop.is_set() is False


def test_throttle_skips_within_window(monkeypatch):
    monkeypatch.setattr(rd, "SELF_UPDATE", True)
    monkeypatch.setattr(rd, "SELF_UPDATE_CHECK_S", 5.0)
    called = {"n": 0}

    def fake_version():
        called["n"] += 1
        return "1.21.8"

    monkeypatch.setattr(rd, "_runtime_version", fake_version)
    d = rd.JudgeDaemon(session_key="k", sock_path="/tmp/uni-selfupdate-test.sock", pool_size=1)
    d._boot_version = "1.21.7"
    called["n"] = 0  # ignore the one resolve __init__ does to stamp _boot_version
    # __init__ stamps _last_version_check = now, so an immediate call is throttled.
    d._maybe_self_update_exit()
    assert called["n"] == 0, "version resolution must be throttled within the window"
    assert d._stop.is_set() is False


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
