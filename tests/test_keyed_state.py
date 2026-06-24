#!/usr/bin/env python3
"""Global, directory + session-keyed state: specs and the goals plan live at
<data_root>/specs/<dir_hash(cwd)>/<session>/, so a new session never inherits a
prior session's state and two repos never collide.

This is the regression suite for the stale-plan bleed: before keying, a prior
session's ./.unifable/goals.json was a directory singleton that any later session
picked up and blocked on.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))


def _with_data(data_dir):
    os.environ["UNIFABLE_DATA"] = data_dir


def _spec():
    return {
        "restated_goal": "do the thing well",
        "goal_seeded": False,
        "acceptance_criteria": [{"check": "true", "evidence": "ok"}],
        "repo_context": [], "prior_art": [], "tasks": [],
    }


def test_different_cwds_isolate_specs():
    """Same session id, two different working dirs -> different dir_hash -> the
    spec written in repo A is invisible from repo B."""
    with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        _with_data(data)
        from spec import dir_hash, load_spec, save_spec, spec_path
        assert dir_hash(a) != dir_hash(b)
        save_spec(a, "S", _spec())
        assert load_spec(a, "S") is not None
        assert load_spec(b, "S") is None
        assert spec_path(a, "S") != spec_path(b, "S")


def test_different_sessions_isolate_specs():
    """Same cwd, two sessions -> isolated specs. The stale-bleed fix: session B
    never sees session A's spec."""
    with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as cwd:
        _with_data(data)
        from spec import load_spec, save_spec
        save_spec(cwd, "A", _spec())
        assert load_spec(cwd, "A") is not None
        assert load_spec(cwd, "B") is None


def test_same_session_resumes_same_spec():
    """A resumed session (same id) re-finds its own spec."""
    with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as cwd:
        _with_data(data)
        from spec import load_spec, save_spec, spec_path
        save_spec(cwd, "RESUME", _spec())
        assert spec_path(cwd, "RESUME") == spec_path(cwd, "RESUME")
        again = load_spec(cwd, "RESUME")
        assert again is not None and again["restated_goal"] == "do the thing well"


def test_goals_plan_keyed_by_session_no_bleed():
    """goals.py writes the plan under the session dir; gate_stop reads the same for
    that session and finds NOTHING for a different session (the bleed regression)."""
    with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as cwd:
        env = dict(os.environ)
        env["UNIFABLE_DATA"] = data
        env["CLAUDE_CODE_SESSION_ID"] = "PLAN_A"
        r = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "goals.py"),
             "create", "--brief", "b", "--goal", "t::o"],
            cwd=cwd, capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        _with_data(data)
        import gate_stop
        assert gate_stop._load_goal_plan(cwd, "PLAN_A") is not None
        assert gate_stop._load_goal_plan(cwd, "PLAN_B") is None  # different session -> no bleed


def test_edit_to_global_spec_path_is_blocked():
    """The spec lives globally now; a direct Edit to it must still be blocked
    (specs are CLI-only)."""
    with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as cwd:
        _with_data(data)
        from spec import spec_path
        target = str(spec_path(cwd, "EDIT"))
        payload = {"tool_name": "Edit", "session_id": "EDIT", "cwd": cwd,
                   "tool_input": {"file_path": target, "old_string": "a", "new_string": "b"}}
        env = dict(os.environ)
        env["UNIFABLE_DATA"] = data
        env["UNIFABLE_GRADE"] = "STANDARD"
        env["UNIFABLE_VERIFY_CITATIONS"] = "0"
        p = subprocess.run([sys.executable, str(REPO / "hooks" / "pre_tool_use.py")],
                           input=json.dumps(payload), capture_output=True, text=True, env=env)
        assert p.returncode == 2, f"global spec edit should be blocked; stderr={p.stderr}"
        assert "spec.py" in p.stderr.lower() or "protected" in p.stderr.lower()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
