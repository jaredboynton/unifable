#!/usr/bin/env python3
"""Tests for spec.py (validate_spec, check_fake_evidence) and
pre_tool_use.py (PROTECTED_PATHS guard, spec gate allow/block).

Invokes pre_tool_use.py via subprocess with crafted stdin, matching the
pattern used by test_gate.py for the other hooks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Locate repo root relative to this file.
REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
SCRIPTS_GATE = REPO / "scripts" / "gate"
PY = sys.executable

# ---------------------------------------------------------------------------
# Import spec module directly for unit tests
# ---------------------------------------------------------------------------

sys.path.insert(0, str(SCRIPTS_GATE))
from spec import (  # noqa: E402
    check_fake_evidence,
    load_spec,
    save_spec,
    spec_path,
    spec_template,
    validate_spec,
)


# ---------------------------------------------------------------------------
# Helper: run pre_tool_use.py via subprocess
# ---------------------------------------------------------------------------

def run_pre_tool(
    payload: dict,
    *,
    spec_gate: str = "0",
    grade: str = "STANDARD",
    env_extra: dict | None = None,
    tmp_root: str | None = None,
) -> tuple[int, dict, str]:
    """Run hooks/pre_tool_use.py with *payload* on stdin.

    Returns (returncode, parsed-stdout-json, stderr).
    """
    env = dict(os.environ)
    env["UNIFABLE_SPEC_GATE"] = spec_gate
    env["UNIFABLE_GRADE"] = grade
    if tmp_root:
        env["UNIFABLE_DATA"] = tmp_root
    if env_extra:
        env.update(env_extra)

    proc = subprocess.run(
        [PY, str(HOOKS / "pre_tool_use.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    try:
        out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        out = {"_raw": proc.stdout}
    return proc.returncode, out, proc.stderr


# ---------------------------------------------------------------------------
# Unit tests: validate_spec
# ---------------------------------------------------------------------------

def test_validate_light_minimal():
    """LIGHT accepts a spec with only restated_goal + 1 acceptance criterion."""
    spec = {
        "restated_goal": "Add a --verbose flag to the CLI.",
        "acceptance_criteria": [
            {"check": "python cli.py --verbose 2>&1 | grep verbose", "evidence": "verbose mode enabled"}
        ],
    }
    ok, reasons = validate_spec(spec, "LIGHT")
    assert ok, reasons


def test_validate_light_missing_goal():
    spec = {
        "restated_goal": "",
        "acceptance_criteria": [{"check": "echo ok", "evidence": "ok"}],
    }
    ok, reasons = validate_spec(spec, "LIGHT")
    assert not ok
    assert any("restated_goal" in r for r in reasons)


def test_validate_light_empty_criteria():
    spec = {
        "restated_goal": "Fix typo in README.",
        "acceptance_criteria": [],
    }
    ok, reasons = validate_spec(spec, "LIGHT")
    assert not ok
    assert any("acceptance_criteria" in r for r in reasons)


def test_validate_standard_passes():
    spec = {
        "restated_goal": "Implement rate-limiting middleware for the /api endpoints.",
        "acceptance_criteria": [
            {"check": "pytest tests/test_rate_limit.py -v", "evidence": "5 passed in 0.4s"}
        ],
    }
    ok, reasons = validate_spec(spec, "STANDARD")
    assert ok, reasons


def test_validate_standard_missing_required():
    """STANDARD without acceptance_criteria fails."""
    spec = {"restated_goal": "Do something."}
    ok, reasons = validate_spec(spec, "STANDARD")
    assert not ok
    assert any("acceptance_criteria" in r for r in reasons)


def test_validate_standard_fake_evidence():
    spec = {
        "restated_goal": "Add auth middleware.",
        "acceptance_criteria": [
            {"check": "pytest tests/test_auth.py", "evidence": "tbd"}
        ],
    }
    ok, reasons = validate_spec(spec, "STANDARD")
    assert not ok
    assert any("placeholder" in r.lower() or "tbd" in r.lower() for r in reasons)


def test_validate_heavy_passes():
    spec = {
        "restated_goal": "Migrate the user table to include a verified_at timestamp.",
        "acceptance_criteria": [
            {"check": "python manage.py test tests.test_migration", "evidence": "1 passed in 0.9s"}
        ],
        "constraints": ["Must be backward-compatible with the read replica."],
        "rejected_alternatives": [
            "Add a separate verified table — rejected: foreign-key overhead at scale.",
            "Use a nullable boolean — rejected: loses migration timestamp precision.",
        ],
    }
    ok, reasons = validate_spec(spec, "HEAVY")
    assert ok, reasons


def test_validate_heavy_missing_constraints():
    spec = {
        "restated_goal": "Rewrite auth to use JWT.",
        "acceptance_criteria": [
            {"check": "pytest tests/test_jwt.py", "evidence": "3 passed"}
        ],
        "constraints": [],
        "rejected_alternatives": [
            "Session cookies — rejected: stateful.",
            "HMAC tokens — rejected: no expiry.",
        ],
    }
    ok, reasons = validate_spec(spec, "HEAVY")
    assert not ok
    assert any("constraints" in r for r in reasons)


def test_validate_heavy_insufficient_rejected_alternatives():
    spec = {
        "restated_goal": "Rewrite auth to use JWT.",
        "acceptance_criteria": [
            {"check": "pytest tests/test_jwt.py", "evidence": "3 passed"}
        ],
        "constraints": ["Must not break existing sessions."],
        "rejected_alternatives": ["Session cookies — rejected: stateful."],
    }
    ok, reasons = validate_spec(spec, "HEAVY")
    assert not ok
    assert any("rejected_alternatives" in r for r in reasons)


def test_validate_unknown_grade():
    spec = spec_template()
    ok, reasons = validate_spec(spec, "EXTREME")
    assert not ok
    assert any("Unknown grade" in r for r in reasons)


# ---------------------------------------------------------------------------
# Unit tests: check_fake_evidence
# ---------------------------------------------------------------------------

def test_fake_markers_detected():
    for marker in ("tbd", "pending", "n/a", "not run", "assumed", "placeholder", "todo"):
        found = check_fake_evidence(f"Test output: {marker}")
        assert marker in found, f"Expected marker '{marker}' to be detected"


def test_fake_markers_case_insensitive():
    assert "tbd" in check_fake_evidence("TBD")
    assert "pending" in check_fake_evidence("PENDING")


def test_real_evidence_clean():
    text = "5 passed in 0.31s (short test run, no warnings)"
    assert check_fake_evidence(text) == []


def test_multiple_markers():
    found = check_fake_evidence("not run — todo later, pending review")
    assert "not run" in found
    assert "todo" in found
    assert "pending" in found


# ---------------------------------------------------------------------------
# Integration tests: pre_tool_use.py subprocess
# ---------------------------------------------------------------------------

def _edit_payload(file_path: str, session_id: str = "sess-abc123", cwd: str = "/work") -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "old_string": "x", "new_string": "y"},
        "tool_response": {"success": True},
        "session_id": session_id,
        "cwd": cwd,
    }


def _bash_payload(cmd: str, session_id: str = "sess-abc123", cwd: str = "/work") -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "tool_response": {"exit_code": 0, "stdout": "ok"},
        "session_id": session_id,
        "cwd": cwd,
    }


# --- Gate OFF (default) ---

def test_gate_off_allows_any_edit():
    """When UNIFABLE_SPEC_GATE=0, all edits pass regardless."""
    rc, out, _ = run_pre_tool(_edit_payload("/work/src/main.py"), spec_gate="0")
    assert rc == 0
    assert out == {}


def test_gate_off_non_write_tool_passes():
    rc, out, _ = run_pre_tool(_bash_payload("echo hi"), spec_gate="0")
    assert rc == 0


# --- PROTECTED_PATHS guard (active even when spec gate is OFF) ---

def test_protected_ledger_blocked():
    """Writes to .unifable/ledger*.json are blocked regardless of spec gate."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(
            os.path.join(cwd, ".unifable", "ledger_abc.json"),
            cwd=cwd,
        )
        rc, out, stderr = run_pre_tool(payload, spec_gate="0")
        assert rc == 2
        assert "protected" in stderr.lower() or "unifable" in stderr.lower()


def test_protected_goals_blocked():
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(
            os.path.join(cwd, ".unifable", "goals.json"),
            cwd=cwd,
        )
        rc, _, stderr = run_pre_tool(payload, spec_gate="0")
        assert rc == 2


def test_protected_findings_blocked():
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(
            os.path.join(cwd, ".unifable", "findings.json"),
            cwd=cwd,
        )
        rc, _, _ = run_pre_tool(payload, spec_gate="0")
        assert rc == 2


def test_protected_state_subdir_blocked():
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(
            os.path.join(cwd, ".unifable", "state", "something.json"),
            cwd=cwd,
        )
        rc, _, _ = run_pre_tool(payload, spec_gate="0")
        assert rc == 2


def test_spec_file_allowed_by_model():
    """The model IS allowed to write .unifable/spec/<task>.json."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(
            os.path.join(cwd, ".unifable", "spec", "sess-abc123.json"),
            session_id="sess-abc123",
            cwd=cwd,
        )
        rc, out, _ = run_pre_tool(payload, spec_gate="0")
        assert rc == 0


def test_normal_src_file_not_protected():
    """Edits to regular project files are never blocked by PROTECTED_PATHS."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(
            os.path.join(cwd, "src", "app.py"),
            cwd=cwd,
        )
        rc, _, _ = run_pre_tool(payload, spec_gate="0")
        assert rc == 0


def test_path_traversal_blocked():
    """A path like .unifable/spec/../../goals.json must be blocked after resolve."""
    with tempfile.TemporaryDirectory() as cwd:
        # Build the traversal path as a string (don't resolve it here)
        traversal = os.path.join(cwd, ".unifable", "spec", "..", "..", ".unifable", "goals.json")
        payload = _edit_payload(traversal, cwd=cwd)
        rc, _, _ = run_pre_tool(payload, spec_gate="0")
        assert rc == 2


# --- Spec gate: LIGHT waives spec requirement ---

def test_light_grade_waives_spec():
    """LIGHT grade: no spec needed, writes always pass."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(os.path.join(cwd, "src", "main.py"), cwd=cwd)
        rc, out, _ = run_pre_tool(payload, spec_gate="1", grade="LIGHT")
        assert rc == 0
        assert out == {}


# --- Spec gate: STANDARD blocks when no spec exists ---

def test_standard_no_spec_blocks():
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(
            os.path.join(cwd, "src", "main.py"),
            session_id="no-spec-session",
            cwd=cwd,
        )
        rc, _, stderr = run_pre_tool(payload, spec_gate="1", grade="STANDARD")
        assert rc == 2
        assert "spec" in stderr.lower()


# --- Spec gate: STANDARD allows when valid spec exists ---

def test_standard_valid_spec_allows():
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "test-session-001"
        good_spec = {
            "restated_goal": "Add health-check endpoint to the API server.",
            "acceptance_criteria": [
                {
                    "check": "curl -s http://localhost:8000/health | jq .status",
                    "evidence": '"ok"',
                }
            ],
        }
        save_spec(cwd, session_id, good_spec)
        payload = _edit_payload(
            os.path.join(cwd, "src", "server.py"),
            session_id=session_id,
            cwd=cwd,
        )
        rc, out, _ = run_pre_tool(payload, spec_gate="1", grade="STANDARD")
        assert rc == 0
        assert out == {}


# --- Spec gate: invalid spec blocks ---

def test_standard_invalid_spec_blocks():
    """A spec with fake evidence is rejected even when the file exists."""
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "test-session-002"
        bad_spec = {
            "restated_goal": "Add auth endpoint.",
            "acceptance_criteria": [
                {"check": "pytest tests/test_auth.py", "evidence": "tbd"}
            ],
        }
        save_spec(cwd, session_id, bad_spec)
        payload = _edit_payload(
            os.path.join(cwd, "src", "auth.py"),
            session_id=session_id,
            cwd=cwd,
        )
        rc, _, stderr = run_pre_tool(payload, spec_gate="1", grade="STANDARD")
        assert rc == 2
        assert "spec" in stderr.lower()


# --- Spec gate: HEAVY requires constraints + rejected_alternatives ---

def test_heavy_missing_constraints_blocks():
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "heavy-session-001"
        spec = {
            "restated_goal": "Migrate DB schema.",
            "acceptance_criteria": [
                {"check": "python manage.py test", "evidence": "1 passed"}
            ],
            "constraints": [],
            "rejected_alternatives": ["alt1 — rejected.", "alt2 — rejected."],
        }
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(
            os.path.join(cwd, "migrations", "0002.py"),
            session_id=session_id,
            cwd=cwd,
        )
        rc, _, stderr = run_pre_tool(payload, spec_gate="1", grade="HEAVY")
        assert rc == 2
        assert "constraint" in stderr.lower()


def test_heavy_valid_spec_allows():
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "heavy-session-002"
        spec = {
            "restated_goal": "Migrate DB schema to add verified_at.",
            "acceptance_criteria": [
                {"check": "python manage.py test", "evidence": "1 passed in 0.9s"}
            ],
            "constraints": ["Must be backward-compatible."],
            "rejected_alternatives": [
                "Separate table — rejected: join overhead.",
                "Nullable boolean — rejected: loses timestamp.",
            ],
        }
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(
            os.path.join(cwd, "migrations", "0002.py"),
            session_id=session_id,
            cwd=cwd,
        )
        rc, out, _ = run_pre_tool(payload, spec_gate="1", grade="HEAVY")
        assert rc == 0


# --- Non-write tool is never blocked ---

def test_bash_not_blocked_by_spec_gate():
    with tempfile.TemporaryDirectory() as cwd:
        payload = _bash_payload("echo test", cwd=cwd)
        rc, out, _ = run_pre_tool(payload, spec_gate="1", grade="STANDARD")
        assert rc == 0


# --- Fail open on bad input ---

def test_empty_stdin_fails_open():
    """Empty / malformed JSON must not crash the hook."""
    env = dict(os.environ)
    env["UNIFABLE_SPEC_GATE"] = "1"
    env["UNIFABLE_GRADE"] = "STANDARD"
    proc = subprocess.run(
        [PY, str(HOOKS / "pre_tool_use.py")],
        input="",
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout or "{}")
    assert out == {}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  OK  {fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
