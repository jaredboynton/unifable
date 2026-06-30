#!/usr/bin/env python3
"""Tests for the deterministic self-contradiction detector (Fix C).

A completion-gate task check that requires an action the research-phase shell
allowlist blocks is a gate self-contradiction. The detector must flag it
deterministically so the agent gets a judge-independent escape instead of an
infinite loop (the access-token-migration deadlock).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "gate"))

from check_satisfiability import detect_self_contradiction  # noqa: E402


def _task(tid, check):
    return {"id": tid, "title": f"task {tid}", "check": check, "status": "pending"}


def test_no_contradiction_when_check_uses_allowed_actions():
    """Fix A made branch switching allowed; a check that switches then verifies is fine."""
    spec = {"tasks": [_task("T1", "git checkout work && git rev-parse --abbrev-ref HEAD")]}
    assert detect_self_contradiction(spec, ["T1"]) == ""


def test_no_contradiction_when_check_verifies_ref_only():
    """A check that verifies a branch ref without a blocked action is not a contradiction."""
    spec = {"tasks": [_task("T1", "git show-ref --verify refs/heads/work")]}
    assert detect_self_contradiction(spec, ["T1"]) == ""


def test_contradiction_when_check_runs_blocked_git_subcommand():
    """A check that runs `git reset`/`git merge`/`git rebase` requires a blocked action."""
    spec = {"tasks": [_task("T1", "git reset --hard HEAD~1 && git status")]}
    notice = detect_self_contradiction(spec, ["T1"])
    assert notice
    assert "git reset" in notice
    assert "self-contradiction" in notice.lower()


def test_contradiction_for_destructive_checkout_shape():
    """A check that runs `git checkout -- file` (pathspec) requires a blocked shape."""
    spec = {"tasks": [_task("T2", "git checkout -- src/app.py && test -f src/app.py")]}
    notice = detect_self_contradiction(spec, ["T2"])
    assert notice
    assert "destructive checkout/switch" in notice


def test_contradiction_for_checkout_detach():
    spec = {"tasks": [_task("T3", "git checkout --detach && git rev-parse HEAD")]}
    notice = detect_self_contradiction(spec, ["T3"])
    assert notice


def test_contradiction_skips_completed_tasks():
    """Only incomplete task ids are examined."""
    spec = {
        "tasks": [
            _task("T1", "git show-ref --verify refs/heads/work"),
            _task("T2", "git reset --hard HEAD"),
        ]
    }
    # Only T1 is incomplete -> no contradiction (T2 not scanned).
    assert detect_self_contradiction(spec, ["T1"]) == ""
    # T2 incomplete -> contradiction flagged.
    assert detect_self_contradiction(spec, ["T2"])


def test_contradiction_lists_all_hits():
    spec = {
        "tasks": [
            _task("T1", "git reset --hard HEAD"),
            _task("T2", "git merge feature"),
        ]
    }
    notice = detect_self_contradiction(spec, ["T1", "T2"])
    assert notice
    assert "git reset" in notice
    assert "git merge" in notice


def test_detector_fail_closed_on_bad_input():
    assert detect_self_contradiction(None, ["T1"]) == ""
    assert detect_self_contradiction({}, None) == ""
    assert detect_self_contradiction({"tasks": "not-a-list"}, ["T1"]) == ""
