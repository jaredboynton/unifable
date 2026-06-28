#!/usr/bin/env python3
# cleanup-traps: not-applicable -- this test patches subprocess.Popen with a mock; it never spawns a real child process.
"""Background breaker-release lane: dispatch gating + drain convergence.

The disarm (lift) moved OFF the PostToolUse hot path into a detached worker
(breaker_release_lane). These tests pin the convergence invariants the design
relies on, independent of the gate_post_tool integration:

  - lease debounce: at most one in-flight disarm per breaker per TTL, so a burst
    of release tools cannot fork a process storm;
  - spawn kill-switch (UNIFABLE_BREAKER_RELEASE_BG=0) is honored;
  - drain is read-and-clear: the enqueued message surfaces exactly once;
  - a spawn failure releases the lease so a later tool retries.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import breaker_release_lane  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_project_root_override():
    # _payload pins UNIFABLE_PROJECT_ROOT; clear it after each test so the
    # override never leaks into sibling test files on the same xdist worker.
    yield
    os.environ.pop("UNIFABLE_PROJECT_ROOT", None)


def _payload(sess: str, cwd: str) -> dict:
    # Pin the project root so rel_key resolution never shells out to git
    # (patching subprocess.Popen would otherwise intercept that probe and
    # break canonical_project_root). spec_io honors this override first.
    os.environ["UNIFABLE_PROJECT_ROOT"] = cwd
    return {
        "session_id": sess,
        "cwd": cwd,
        "tool_name": "Read",
        "tool_input": {"file_path": str(Path(cwd) / "foo.py")},
        "tool_response": {"content": "evidence"},
    }


def test_lease_debounces_second_spawn():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        os.environ["UNIFABLE_DATA"] = dd
        payload = _payload("RL1", cwd)
        with patch("subprocess.Popen") as popen:
            first = breaker_release_lane.spawn_release_job(payload, "Read", "fresh")
            second = breaker_release_lane.spawn_release_job(payload, "Read", "fresh")
        assert first is True
        assert second is False  # lease held by the in-flight job
        assert popen.call_count == 1


def test_spawn_disabled_by_env():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        os.environ["UNIFABLE_DATA"] = dd
        os.environ["UNIFABLE_BREAKER_RELEASE_BG"] = "0"
        try:
            with patch("subprocess.Popen") as popen:
                spawned = breaker_release_lane.spawn_release_job(_payload("RL2", cwd), "Read", "fresh")
            assert spawned is False
            assert popen.call_count == 0
        finally:
            os.environ.pop("UNIFABLE_BREAKER_RELEASE_BG", None)


def test_spawn_failure_releases_lease():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        os.environ["UNIFABLE_DATA"] = dd
        os.environ.pop("UNIFABLE_BREAKER_RELEASE_BG", None)
        payload = _payload("RL3", cwd)
        with patch("subprocess.Popen", side_effect=OSError("boom")):
            failed = breaker_release_lane.spawn_release_job(payload, "Read", "fresh")
        assert failed is False
        # Lease was released on failure: a later tool can spawn again.
        with patch("subprocess.Popen") as popen:
            retry = breaker_release_lane.spawn_release_job(payload, "Read", "fresh")
        assert retry is True
        assert popen.call_count == 1


def test_drain_is_read_and_clear():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        os.environ["UNIFABLE_DATA"] = dd
        import db

        payload = _payload("RL4", cwd)
        key = breaker_release_lane._rel_key(payload)
        assert key
        db.breaker_release_push(key, "breaker open: proceed")
        assert breaker_release_lane.drain_pending_release(payload) == "breaker open: proceed"
        assert breaker_release_lane.drain_pending_release(payload) == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
