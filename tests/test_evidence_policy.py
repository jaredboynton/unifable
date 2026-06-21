#!/usr/bin/env python3
"""Tests for the single policy boundary (scripts/gate/evidence_policy.py) and the
grade-driven observation gate (scripts/gate/verify_state.py).

Covers the consolidation of the two vocabularies:
  - quick/normal/deep stay classifier/UX labels,
  - LIGHT/STANDARD/HEAVY stay the canonical enforcement grade,
  - the mode->grade map and the read-time precedence live in ONE place.

Also asserts the corrections the critics flagged: a fresh/never-classified ledger
defaults to STANDARD (not waived), env override precedence is single-sourced, and
the observation gate keys off the resolved grade (HEAVY-only), so UNIFABLE_GRADE
reaches it consistently.

Runs under pytest or standalone (python3 tests/test_evidence_policy.py).
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

from evidence_policy import (  # noqa: E402
    MODE_TO_GRADE,
    Policy,
    grade_for_mode,
    higher_mode,
    policy_for_grade,
    policy_for_mode,
    resolve_grade,
    resolve_policy,
)
from verify_state import should_block_stop  # noqa: E402


# --- mode -> grade map -------------------------------------------------------

def test_grade_for_mode_canonical_map():
    assert grade_for_mode("quick") == "LIGHT"
    assert grade_for_mode("normal") == "STANDARD"
    assert grade_for_mode("deep") == "HEAVY"


def test_grade_for_mode_unknown_defaults_standard():
    assert grade_for_mode("") == "STANDARD"
    assert grade_for_mode(None) == "STANDARD"
    assert grade_for_mode("banana") == "STANDARD"


def test_grade_for_mode_is_case_insensitive():
    assert grade_for_mode("DEEP") == "HEAVY"
    assert grade_for_mode("  Normal ") == "STANDARD"


# --- Policy object -----------------------------------------------------------

def test_policy_properties():
    light = policy_for_mode("quick")
    standard = policy_for_mode("normal")
    heavy = policy_for_mode("deep")

    assert light.grade == "LIGHT" and light.waived and not light.requires_spec
    assert not light.blocks_unverified_stop

    assert standard.grade == "STANDARD" and standard.requires_spec
    assert not standard.blocks_unverified_stop

    assert heavy.grade == "HEAVY" and heavy.requires_spec
    assert heavy.blocks_unverified_stop


def test_policy_for_grade_normalizes_and_defaults():
    assert policy_for_grade("heavy").grade == "HEAVY"
    assert policy_for_grade("nonsense").grade == "STANDARD"
    assert Policy("EXTREME").grade == "STANDARD"  # unknown grade -> safe default


# --- resolve_grade precedence ------------------------------------------------

def test_env_override_wins():
    ledger = {"active_task": "k", "task_mode": "quick", "grade": "LIGHT"}
    assert resolve_grade(ledger, "HEAVY") == "HEAVY"


def test_invalid_env_falls_through_to_task_mode():
    ledger = {"active_task": "k", "task_mode": "deep", "grade": "HEAVY"}
    assert resolve_grade(ledger, "BOGUS") == "HEAVY"
    assert resolve_grade(ledger, "") == "HEAVY"
    assert resolve_grade(ledger, None) == "HEAVY"


def test_active_task_mode_derives_grade():
    assert resolve_grade({"active_task": "k", "task_mode": "normal"}) == "STANDARD"
    assert resolve_grade({"active_task": "k", "task_mode": "deep"}) == "HEAVY"
    assert resolve_grade({"active_task": "k", "task_mode": "quick"}) == "LIGHT"


def test_fresh_ledger_without_active_task_defaults_standard():
    # No prompt processed yet (no active_task): the default 'quick' task_mode must
    # NOT waive the gate. This keeps the evidence gate enforcing by default.
    assert resolve_grade({"task_mode": "quick"}) == "STANDARD"
    assert resolve_grade({}) == "STANDARD"
    assert resolve_grade(None) == "STANDARD"


def test_legacy_grade_fallback_when_no_active_task():
    # Old ledger shape: only a persisted grade, no active_task.
    assert resolve_grade({"grade": "HEAVY"}) == "HEAVY"
    assert resolve_grade({"grade": "LIGHT"}) == "LIGHT"


def test_active_task_with_invalid_mode_falls_back_to_legacy_grade():
    assert resolve_grade({"active_task": "k", "task_mode": "weird", "grade": "HEAVY"}) == "HEAVY"


def test_resolve_policy_carries_task_mode():
    pol = resolve_policy({"active_task": "k", "task_mode": "deep"})
    assert pol.grade == "HEAVY" and pol.task_mode == "deep"


# --- higher_mode escalation (no-downgrade pin) -------------------------------

def test_higher_mode_escalates_never_downgrades():
    assert higher_mode("deep", "quick") == "deep"
    assert higher_mode("quick", "deep") == "deep"
    assert higher_mode("normal", "deep") == "deep"
    assert higher_mode("normal", "quick") == "normal"
    assert higher_mode("quick", "quick") == "quick"


def test_higher_mode_handles_unknowns():
    assert higher_mode("weird", "normal") == "normal"
    assert higher_mode(None, "quick") == "quick"


# --- observation gate keys off grade (HEAVY-only) ----------------------------

def _changed_unverified() -> dict:
    return {"changed_files_seen": True, "change_kinds": ["code"],
            "verification_results": [], "stop_blocks": 0}


def test_observation_gate_blocks_only_heavy():
    led = _changed_unverified()
    assert should_block_stop(led, "HEAVY")[0] is True
    assert should_block_stop(led, "STANDARD")[0] is False
    assert should_block_stop(led, "LIGHT")[0] is False


def test_observation_gate_grade_none_derives_from_task_mode():
    # Back-compat: no grade passed -> derive from task_mode classification.
    deep = {**_changed_unverified(), "task_mode": "deep"}
    normal = {**_changed_unverified(), "task_mode": "normal"}
    quick = {**_changed_unverified(), "task_mode": "quick"}
    assert should_block_stop(deep)[0] is True
    assert should_block_stop(normal)[0] is False
    assert should_block_stop(quick)[0] is False


def test_observation_gate_docs_only_and_verified_do_not_block():
    docs = {"changed_files_seen": True, "change_kinds": ["docs"],
            "verification_results": [], "stop_blocks": 0}
    assert should_block_stop(docs, "HEAVY")[0] is False
    verified = {**_changed_unverified(), "verification_results": [{"success": True}]}
    assert should_block_stop(verified, "HEAVY")[0] is False


def test_observation_gate_respects_stop_block_cap():
    capped = {**_changed_unverified(), "stop_blocks": 2}
    assert should_block_stop(capped, "HEAVY")[0] is False


# --- integration: env override reaches the observation gate via gate_stop -----

def _run_stop(payload: dict, data_dir: str, grade: str | None) -> dict:
    env = dict(os.environ)
    env["UNIFABLE_DATA"] = data_dir
    env["UNIFABLE_VERIFY_CITATIONS"] = "0"
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.pop("CODEX_THREAD_ID", None)
    if grade is not None:
        env["UNIFABLE_GRADE"] = grade
    else:
        env.pop("UNIFABLE_GRADE", None)
    p = subprocess.run([sys.executable, str(REPO / "hooks" / "gate_stop.py")],
                       input=json.dumps(payload), capture_output=True, text=True, env=env)
    try:
        return json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        return {}


def _seed_changed_session(cwd: str, dd: str, sess: str, prompt: str) -> None:
    """Classify the prompt, mark a code edit, and write a HEAVY-valid spec at the
    session key so only the observation gate decides."""
    def run(hook: str, payload: dict) -> None:
        env = dict(os.environ)
        env["UNIFABLE_DATA"] = dd
        env["UNIFABLE_VERIFY_CITATIONS"] = "0"
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("CODEX_THREAD_ID", None)
        subprocess.run([sys.executable, str(REPO / "hooks" / hook)],
                       input=json.dumps(payload), capture_output=True, text=True, env=env)

    run("gate_prompt.py", {"prompt": prompt, "session_id": sess, "cwd": cwd})
    run("gate_post_tool.py", {"tool_name": "Edit", "session_id": sess, "cwd": cwd,
                              "tool_input": {"file_path": os.path.join(cwd, "src", "x.py"),
                                             "old_string": "a", "new_string": "b"}})
    old = os.environ.get("UNIFABLE_DATA")
    os.environ["UNIFABLE_DATA"] = dd
    try:
        from spec import save_spec
        save_spec(cwd, sess, {
            "restated_goal": "fixture goal",
            "acceptance_criteria": [{"check": "pytest -q", "evidence": "5 passed in 0.4s"}],
            "repo_context": [{"cite": "src/x.py:1", "why": "fixture passage"}],
            "prior_art": [{"cite": "https://example.com/doc", "why": "fixture source"}],
            "constraints": ["fixture constraint"],
            "rejected_alternatives": ["alt a rejected: reason.", "alt b rejected: reason."],
        })
    finally:
        if old is None:
            os.environ.pop("UNIFABLE_DATA", None)
        else:
            os.environ["UNIFABLE_DATA"] = old


def test_env_grade_override_reaches_observation_gate():
    """A normal task (STANDARD) that changed code without verification is NOT
    blocked by the observation gate; forcing UNIFABLE_GRADE=HEAVY blocks it. This
    proves the env override is single-sourced through evidence_policy and now
    consistently reaches the observation gate."""
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess, prompt = "OBS", "fix the login bug in the parser"
        _seed_changed_session(cwd, dd, sess, prompt)

        payload = {"session_id": sess, "cwd": cwd, "stop_hook_active": False}
        out_std = _run_stop(payload, dd, grade="STANDARD")
        assert out_std.get("decision") != "block", out_std

        out_heavy = _run_stop(payload, dd, grade="HEAVY")
        assert out_heavy.get("decision") == "block", out_heavy
        assert "verification" in (out_heavy.get("reason") or "").lower()


if __name__ == "__main__":
    fails = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"  [OK] {_name}")
            except AssertionError as e:
                fails += 1
                print(f"  [FAIL] {_name}: {e}")
    print("RESULT:", "all pass" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
