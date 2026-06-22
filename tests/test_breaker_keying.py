#!/usr/bin/env python3
"""Prompt-hash keying (locked-until-complete), CLI-only spec protection, and the
Stop breaker (block until all tasks validated), exercised through the real hooks.

The judge is not called here: the breaker reads task `status` from the spec, so we
write spec states directly (the spec protection is tool-level, not filesystem) and
assert the gate's block/allow behavior. The live judge path is covered separately.

Runs under pytest or standalone (python3 tests/test_breaker_keying.py).
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
from ledger import load_ledger  # noqa: E402


def _key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()[:16]


def _run(hook: str, payload: dict, data_dir: str, grade: str | None = None) -> tuple[int, dict, str]:
    env = dict(os.environ)
    env["UNIFABLE_DATA"] = data_dir
    # Breaker/keying harness: citation truth-checking is covered in
    # tests/test_citation_verify.py; isolate it here.
    env["UNIFABLE_VERIFY_CITATIONS"] = "0"
    if grade:
        env["UNIFABLE_GRADE"] = grade
    p = subprocess.run(
        [sys.executable, str(REPO / "hooks" / hook)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )
    try:
        out = json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError:
        out = {}
    return p.returncode, out, p.stderr


def _ledger_active(session: str, cwd: str, data_dir: str) -> str | None:
    old = os.environ.get("UNIFABLE_DATA")
    os.environ["UNIFABLE_DATA"] = data_dir
    try:
        return load_ledger({"session_id": session, "cwd": cwd}).get("active_task")
    finally:
        if old is None:
            os.environ.pop("UNIFABLE_DATA", None)
        else:
            os.environ["UNIFABLE_DATA"] = old


def _write_spec(cwd: str, key: str, task_status: str, data_dir: str) -> None:
    """Seed a session spec at the keyed global path (key = the session id)."""
    spec = {
        "restated_goal": "do the thing",
        "goal_seeded": False,
        "acceptance_criteria": [],
        "tasks": [{"id": "T1", "title": "t1", "check": "true", "status": task_status,
                   "exit": 0, "output": "ok", "judge_verdict": 1, "judge_reason": "ok"}],
        "repo_context": [{"cite": "a.py:1", "why": "why it matters"}],
        "prior_art": [{"cite": "http://example.com/doc", "why": "fixture source"}],
        "constraints": [], "rejected_alternatives": [],
    }
    old = os.environ.get("UNIFABLE_DATA")
    os.environ["UNIFABLE_DATA"] = data_dir
    try:
        from spec import save_spec
        save_spec(cwd, key, spec)
    finally:
        if old is None:
            os.environ.pop("UNIFABLE_DATA", None)
        else:
            os.environ["UNIFABLE_DATA"] = old


def _spec_exists(cwd: str, key: str, data_dir: str) -> bool:
    old = os.environ.get("UNIFABLE_DATA")
    os.environ["UNIFABLE_DATA"] = data_dir
    try:
        from spec import load_spec
        return load_spec(cwd, key) is not None
    finally:
        if old is None:
            os.environ.pop("UNIFABLE_DATA", None)
        else:
            os.environ["UNIFABLE_DATA"] = old


def test_session_keying_one_spec_per_session():
    """The spec is keyed by session, not prompt: two different prompts in the same
    session share ONE spec (at the session key), and `active_task` tracks the latest
    prompt hash for the breaker debounce -- it no longer keys the spec."""
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess = "S1"
        # Non-LIGHT prompts so the hook auto-creates the scaffold.
        p1, p2 = "implement the parser feature", "refactor the auth module"
        _run("gate_prompt.py", {"prompt": p1, "session_id": sess, "cwd": cwd}, dd)
        # The auto-created scaffold is at the SESSION key, not the prompt hash.
        assert _spec_exists(cwd, sess, dd)
        assert not _spec_exists(cwd, _key(p1), dd)
        assert _ledger_active(sess, cwd, dd) == _key(p1)
        # A new prompt in the same session reuses the same spec and re-points
        # active_task to the new prompt hash (breaker debounce key).
        _run("gate_prompt.py", {"prompt": p2, "session_id": sess, "cwd": cwd}, dd)
        assert _ledger_active(sess, cwd, dd) == _key(p2)
        assert _spec_exists(cwd, sess, dd)


def test_spec_files_are_cli_only():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        payload = {"tool_name": "Edit", "cwd": cwd,
                   "tool_input": {"file_path": os.path.join(cwd, ".unifable", "spec", "x.json"),
                                  "old_string": "a", "new_string": "b"}}
        rc, _, stderr = _run("pre_tool_use.py", payload, dd, grade="STANDARD")
        assert rc == 2
        assert "spec.py" in stderr.lower() or "protected" in stderr.lower()


def test_breaker_blocks_until_validated():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess, prompt = "S2", "implement the breaker task"
        _run("gate_prompt.py", {"prompt": prompt, "session_id": sess, "cwd": cwd}, dd)
        key = sess  # spec is keyed by session now
        # Pending task -> breaker CLOSED -> Stop blocked.
        _write_spec(cwd, key, "pending", dd)
        rc, out, _ = _run("gate_stop.py", {"session_id": sess, "cwd": cwd, "stop_hook_active": False}, dd, grade="STANDARD")
        assert out.get("decision") == "block"
        assert "breaker" in (out.get("reason") or "").lower()
        # Validated task -> breaker not the blocker anymore.
        _write_spec(cwd, key, "validated", dd)
        rc2, out2, _ = _run("gate_stop.py", {"session_id": sess, "cwd": cwd, "stop_hook_active": False}, dd, grade="STANDARD")
        assert "breaker" not in (out2.get("reason") or "").lower()


def test_impl_edit_allowed_after_appendonly_spec():
    """No-brick: the hook auto-creates the spec; the agent fills it append-only
    (add-task + cite) into a gate-valid task-spec, then an impl edit on the active
    task is allowed (rc 0). Creation is the hook's job -- the agent never runs create."""
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        sess, prompt = "S3", "fix the parser bug"
        _run("gate_prompt.py", {"prompt": prompt, "session_id": sess, "cwd": cwd}, dd)
        key = sess  # the agent drives the spec at the session key

        def _spec(*a):
            env = dict(os.environ)
            env["UNIFABLE_DATA"] = dd
            env["CLAUDE_CODE_SESSION_ID"] = key
            return subprocess.run(
                [sys.executable, str(REPO / "scripts" / "gate" / "spec.py"), *a],
                capture_output=True, text=True, env=env, cwd=cwd,
            )

        # FIRST action: restate the seeded goal in the agent's own words (the gate
        # stays blocked until goal_seeded is cleared).
        r0 = _spec("restate", "--goal", "make the parser tolerate empty and malformed input")
        assert r0.returncode == 0, r0.stderr
        r1 = _spec("add-task", "--title", "parser handles empty input", "--check", "true")
        assert r1.returncode == 0, r1.stderr
        r2 = _spec("cite", "--repo-context", "src/parser.py:10::where parsing starts",
                   "--prior-art", "http://example.com/grammar::grammar reference")
        assert r2.returncode == 0, r2.stderr
        payload = {"tool_name": "Edit", "session_id": sess, "cwd": cwd,
                   "tool_input": {"file_path": os.path.join(cwd, "src", "parser.py"),
                                  "old_string": "a", "new_string": "b"}}
        rc, out, stderr = _run("pre_tool_use.py", payload, dd, grade="STANDARD")
        assert rc == 0, f"impl edit should be allowed after append-only spec authoring; stderr={stderr}"


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
