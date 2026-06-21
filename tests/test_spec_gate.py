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
    is_path_line,
    is_source_url,
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
    spec_gate: str | None = "0",
    evidence_gate: str | None = "0",
    grade: str = "STANDARD",
    env_extra: dict | None = None,
    tmp_root: str | None = None,
) -> tuple[int, dict, str]:
    """Run hooks/pre_tool_use.py with *payload* on stdin.

    The evidence gate is unconditional: UNIFABLE_SPEC_GATE / UNIFABLE_EVIDENCE_GATE
    no longer disable it. The spec_gate / evidence_gate params are retained only so
    call sites can still set those envs (used to prove the removed escape is now
    ignored). Grade (LIGHT waives) and whether a valid spec exists are what gate.

    Returns (returncode, parsed-stdout-json, stderr).
    """
    env = dict(os.environ)
    env.pop("UNIFABLE_SPEC_GATE", None)
    env.pop("UNIFABLE_EVIDENCE_GATE", None)
    if spec_gate is not None:
        env["UNIFABLE_SPEC_GATE"] = spec_gate
    if evidence_gate is not None:
        env["UNIFABLE_EVIDENCE_GATE"] = evidence_gate
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
# Citation-format helpers
# ---------------------------------------------------------------------------

def test_is_path_line():
    assert is_path_line("src/app.py:42")
    assert is_path_line("a/b/c.py:10-20")
    assert is_path_line("hooks/gate_stop.py:5")
    assert not is_path_line("src/app.py")            # no line number
    assert not is_path_line("https://example.com:8080")  # URL, not a code citation
    assert not is_path_line("")
    assert not is_path_line(None)


def test_is_source_url():
    assert is_source_url("https://arxiv.org/abs/2309.11495")
    assert is_source_url("http://example.com/x")
    assert not is_source_url("src/app.py:42")
    assert not is_source_url("example.com")
    assert not is_source_url("")


# ---------------------------------------------------------------------------
# Evidence gate: validate_spec(require_evidence=True)
# ---------------------------------------------------------------------------

def _standard_spec_with_evidence() -> dict:
    return {
        "restated_goal": "Add rate-limiting middleware to /api endpoints.",
        "acceptance_criteria": [
            {"check": "pytest tests/test_rate_limit.py -v", "evidence": "5 passed in 0.4s"}
        ],
        "must_read": [
            {"cite": "src/middleware.py:88", "why": "rate-limit hook attaches here"},
            {"cite": "src/router.py:12-20", "why": "endpoint registration the middleware wraps"},
        ],
        "prior_art": ["https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429"],
    }


def test_evidence_off_is_backward_compatible():
    """require_evidence defaults False: a spec with no must_read still passes."""
    spec = {
        "restated_goal": "Add a flag.",
        "acceptance_criteria": [{"check": "echo ok", "evidence": "ok"}],
    }
    ok, reasons = validate_spec(spec, "STANDARD")
    assert ok, reasons
    ok2, _ = validate_spec(spec, "STANDARD", require_evidence=False)
    assert ok2


def test_evidence_standard_requires_must_read():
    spec = {
        "restated_goal": "Add a flag.",
        "acceptance_criteria": [{"check": "echo ok", "evidence": "ok"}],
    }
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("must_read" in r for r in reasons)


def test_evidence_standard_passes_with_must_read():
    ok, reasons = validate_spec(_standard_spec_with_evidence(), "STANDARD", require_evidence=True)
    assert ok, reasons


def test_evidence_must_read_malformed_blocks():
    spec = _standard_spec_with_evidence()
    spec["must_read"] = [{"cite": "src/middleware.py", "why": "the hook"}]  # missing :line
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("must_read" in r and "path:line" in r for r in reasons)


def test_evidence_must_read_placeholder_blocks():
    spec = _standard_spec_with_evidence()
    spec["must_read"] = [{"cite": "src/app.py:1", "why": "tbd"}]
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("must_read" in r for r in reasons)


def test_evidence_must_read_requires_why():
    """A must_read citation with no 'why' rationale is rejected."""
    spec = _standard_spec_with_evidence()
    spec["must_read"] = [{"cite": "src/app.py:42", "why": ""}]
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("must_read" in r and "why" in r for r in reasons)


def test_evidence_standard_requires_prior_art():
    """prior_art (source URL) is required from STANDARD up, not only HEAVY."""
    spec = _standard_spec_with_evidence()
    spec.pop("prior_art", None)
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("prior_art" in r for r in reasons)


def test_evidence_light_exempt():
    """LIGHT is exempt from citation requirements even when require_evidence=True."""
    spec = {
        "restated_goal": "Fix a typo.",
        "acceptance_criteria": [{"check": "echo ok", "evidence": "ok"}],
    }
    ok, reasons = validate_spec(spec, "LIGHT", require_evidence=True)
    assert ok, reasons


def test_evidence_heavy_requires_prior_art():
    spec = {
        "restated_goal": "Rewrite auth to use JWT.",
        "acceptance_criteria": [{"check": "pytest tests/test_jwt.py", "evidence": "3 passed"}],
        "constraints": ["Must not break existing sessions."],
        "rejected_alternatives": ["Session cookies — rejected: stateful.", "HMAC — rejected: no expiry."],
        "must_read": [{"cite": "src/auth.py:30", "why": "auth entrypoint being rewritten"}],
        # prior_art missing
    }
    ok, reasons = validate_spec(spec, "HEAVY", require_evidence=True)
    assert not ok
    assert any("prior_art" in r for r in reasons)


def test_evidence_heavy_passes_with_prior_art():
    spec = {
        "restated_goal": "Rewrite auth to use JWT.",
        "acceptance_criteria": [{"check": "pytest tests/test_jwt.py", "evidence": "3 passed in 0.2s"}],
        "constraints": ["Must not break existing sessions."],
        "rejected_alternatives": ["Session cookies — rejected: stateful.", "HMAC — rejected: no expiry."],
        "must_read": [
            {"cite": "src/auth.py:30", "why": "auth entrypoint being rewritten"},
            {"cite": "src/session.py:5-9", "why": "session lifecycle the JWT must preserve"},
        ],
        "prior_art": ["https://datatracker.ietf.org/doc/html/rfc7519"],
    }
    ok, reasons = validate_spec(spec, "HEAVY", require_evidence=True)
    assert ok, reasons


def test_evidence_heavy_prior_art_must_be_url():
    spec = {
        "restated_goal": "Rewrite auth to use JWT.",
        "acceptance_criteria": [{"check": "pytest tests/test_jwt.py", "evidence": "3 passed"}],
        "constraints": ["Must not break existing sessions."],
        "rejected_alternatives": ["Session cookies — rejected: stateful.", "HMAC — rejected: no expiry."],
        "must_read": [{"cite": "src/auth.py:30", "why": "auth entrypoint being rewritten"}],
        "prior_art": ["some blog I read"],
    }
    ok, reasons = validate_spec(spec, "HEAVY", require_evidence=True)
    assert not ok
    assert any("prior_art" in r and "URL" in r for r in reasons)


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


# --- Removed escape hatch: env no longer disables the gate ---

def test_disable_env_has_no_effect():
    """The escape hatch is removed: setting UNIFABLE_EVIDENCE_GATE=0 / SPEC_GATE=0
    does NOT disable the gate. A STANDARD edit with no spec is still blocked."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(os.path.join(cwd, "src", "main.py"),
                                session_id="disable-noop", cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, spec_gate="0", evidence_gate="0", grade="STANDARD")
        assert rc == 2
        assert "spec" in stderr.lower()


def test_read_bash_passes_without_spec():
    """A read/echo Bash command is allowed even with no spec (only create/mutate is locked)."""
    rc, out, _ = run_pre_tool(_bash_payload("echo hi"), grade="STANDARD")
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


def test_spec_file_blocked_for_model():
    """Specs are CLI-only: a direct Edit/Write to .unifable/spec/<task>.json is
    blocked. The model must mutate specs via spec.py (create/add-task/validate-task),
    so it cannot hand-edit the JSON to delete tasks or fake a validated status."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(
            os.path.join(cwd, ".unifable", "spec", "sess-abc123.json"),
            session_id="sess-abc123",
            cwd=cwd,
        )
        rc, _, stderr = run_pre_tool(payload, spec_gate="0")
        assert rc == 2
        assert "spec.py" in stderr.lower() or "cli-only" in stderr.lower() or "protected" in stderr.lower()


def test_normal_src_file_not_protected():
    """Edits to regular project files are never blocked by PROTECTED_PATHS.

    Uses LIGHT so the spec requirement is waived and only the PROTECTED_PATHS
    logic is exercised."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(
            os.path.join(cwd, "src", "app.py"),
            cwd=cwd,
        )
        rc, _, _ = run_pre_tool(payload, grade="LIGHT")
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
            "must_read": [{"cite": "src/server.py:12", "why": "where routes register"}],
            "prior_art": ["https://datatracker.ietf.org/doc/html/rfc9110"],
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
            "must_read": [{"cite": "migrations/0001.py:1", "why": "prior migration this extends"}],
            "prior_art": ["https://docs.djangoproject.com/en/stable/topics/migrations/"],
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

# --- Evidence gate (UNIFABLE_EVIDENCE_GATE=1) integration ---

def test_evidence_gate_spec_authoring_is_cli_only():
    """No-brick is now the CLI: direct Edit/Write of the spec file is blocked (specs
    are mutated only via spec.py), so an agent cannot hand-author or hand-edit the
    JSON. The gate points the agent at `spec.py create` instead."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(
            os.path.join(cwd, ".unifable", "spec", "brick-sess.json"),
            session_id="brick-sess", cwd=cwd,
        )
        rc, _, stderr = run_pre_tool(
            payload, spec_gate="0", grade="STANDARD",
            env_extra={"UNIFABLE_EVIDENCE_GATE": "1"},
        )
        assert rc == 2, "direct spec authoring must be blocked (CLI-only)"
        assert "spec.py" in stderr.lower() or "protected" in stderr.lower()


def test_evidence_gate_default_on_blocks_uncited_edit():
    """Production default (no gate env set): an uncited edit on a STANDARD task is
    blocked. Proves the gate is ON by default."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(os.path.join(cwd, "src", "main.py"),
                                session_id="default-on-sess", cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, spec_gate=None, evidence_gate=None, grade="STANDARD")
        assert rc == 2, "evidence gate must block uncited edits by default"
        assert "must_read" in stderr or "spec" in stderr.lower()


def test_evidence_gate_escape_hatch_removed():
    """The escape hatch is removed: UNIFABLE_EVIDENCE_GATE=0 no longer disables the
    gate. An uncited STANDARD edit is still blocked."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(os.path.join(cwd, "src", "main.py"),
                                session_id="escape-sess", cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, spec_gate=None, evidence_gate="0", grade="STANDARD")
        assert rc == 2
        assert "spec" in stderr.lower()


def test_evidence_gate_default_on_light_waived():
    """Default-on still waives LIGHT (quick) tasks — trivial edits are not over-gated."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(os.path.join(cwd, "src", "main.py"),
                                session_id="light-sess", cwd=cwd)
        rc, out, _ = run_pre_tool(payload, spec_gate=None, evidence_gate=None, grade="LIGHT")
        assert rc == 0


def test_evidence_gate_blocks_valid_spec_without_must_read():
    """A spec that passes the spec gate is still blocked by the evidence gate when
    it lacks must_read citations."""
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "ev-session-001"
        spec = {
            "restated_goal": "Add health-check endpoint.",
            "acceptance_criteria": [
                {"check": "curl -s localhost:8000/health", "evidence": '"ok"'}
            ],
        }
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(
            os.path.join(cwd, "src", "server.py"), session_id=session_id, cwd=cwd
        )
        rc, _, stderr = run_pre_tool(
            payload, spec_gate="0", grade="STANDARD",
            env_extra={"UNIFABLE_EVIDENCE_GATE": "1"},
        )
        assert rc == 2
        assert "must_read" in stderr


def test_evidence_gate_allows_spec_with_citations():
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "ev-session-002"
        spec = {
            "restated_goal": "Add health-check endpoint.",
            "acceptance_criteria": [
                {"check": "curl -s localhost:8000/health", "evidence": '"ok"'}
            ],
            "must_read": [
                {"cite": "src/server.py:10", "why": "app factory where routes mount"},
                {"cite": "src/routes.py:5-8", "why": "route table the endpoint joins"},
            ],
            "prior_art": ["https://datatracker.ietf.org/doc/html/rfc9110"],
        }
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(
            os.path.join(cwd, "src", "server.py"), session_id=session_id, cwd=cwd
        )
        rc, out, _ = run_pre_tool(
            payload, spec_gate="0", grade="STANDARD",
            env_extra={"UNIFABLE_EVIDENCE_GATE": "1"},
        )
        assert rc == 0
        assert out == {}


def test_spec_only_env_does_not_downgrade():
    """The spec-only mode is removed: UNIFABLE_SPEC_GATE=1 no longer downgrades the
    gate. A citationless spec is still rejected (must_read required)."""
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "ev-session-003"
        spec = {
            "restated_goal": "Add health-check endpoint.",
            "acceptance_criteria": [
                {"check": "curl -s localhost:8000/health", "evidence": '"ok"'}
            ],
        }
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(
            os.path.join(cwd, "src", "server.py"), session_id=session_id, cwd=cwd
        )
        rc, _, stderr = run_pre_tool(payload, spec_gate="1", grade="STANDARD")
        assert rc == 2
        assert "must_read" in stderr


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
# Stop gate: evidence spec required at completion (gate_stop.py)
# ---------------------------------------------------------------------------

def run_stop(
    payload: dict,
    *,
    evidence_gate: str | None = "1",
    spec_gate: str | None = None,
    grade: str | None = None,
    data_dir: str | None = None,
    env_extra: dict | None = None,
) -> dict:
    """Run hooks/gate_stop.py with *payload* on stdin. Returns parsed stdout JSON.

    Inherited gate vars are scrubbed first so the test controls the gate state."""
    env = dict(os.environ)
    for k in ("UNIFABLE_SPEC_GATE", "UNIFABLE_EVIDENCE_GATE", "UNIFABLE_GRADE", "UNIFABLE_HOLDOUT"):
        env.pop(k, None)
    if evidence_gate is not None:
        env["UNIFABLE_EVIDENCE_GATE"] = evidence_gate
    if spec_gate is not None:
        env["UNIFABLE_SPEC_GATE"] = spec_gate
    if grade is not None:
        env["UNIFABLE_GRADE"] = grade
    if data_dir:
        env["UNIFABLE_DATA"] = data_dir
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [PY, str(HOOKS / "gate_stop.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"_raw": proc.stdout, "_err": proc.stderr}


def _blocks(out: dict) -> bool:
    return out.get("decision") == "block"


def test_stop_blocks_when_no_spec_standard():
    """The agent is required to write evidence back: no spec -> stop blocks."""
    with tempfile.TemporaryDirectory() as cwd:
        out = run_stop({"session_id": "st1", "cwd": cwd, "stop_hook_active": False}, grade="STANDARD")
        assert _blocks(out)
        assert "no evidence spec" in out.get("reason", "")


def test_stop_allows_when_no_spec_light():
    """LIGHT (quick) tasks are waived from the stop evidence requirement."""
    with tempfile.TemporaryDirectory() as cwd:
        out = run_stop({"session_id": "st2", "cwd": cwd, "stop_hook_active": False}, grade="LIGHT")
        assert not _blocks(out)


def test_stop_allows_with_valid_spec():
    with tempfile.TemporaryDirectory() as cwd:
        save_spec(cwd, "st3", _standard_spec_with_evidence())
        out = run_stop({"session_id": "st3", "cwd": cwd, "stop_hook_active": False}, grade="STANDARD")
        assert not _blocks(out), out


def test_stop_blocks_invalid_spec():
    with tempfile.TemporaryDirectory() as cwd:
        spec = _standard_spec_with_evidence()
        spec.pop("prior_art", None)  # invalid at STANDARD under the evidence gate
        save_spec(cwd, "st4", spec)
        out = run_stop({"session_id": "st4", "cwd": cwd, "stop_hook_active": False}, grade="STANDARD")
        assert _blocks(out)
        assert "invalid" in out.get("reason", "").lower()


def test_stop_evidence_ignores_loop_guard():
    """The evidence gate is INFINITE: no spec blocks even when stop_hook_active=True."""
    with tempfile.TemporaryDirectory() as cwd:
        out = run_stop({"session_id": "st5", "cwd": cwd, "stop_hook_active": True}, grade="STANDARD")
        assert _blocks(out)


def test_stop_loop_guard_allows_soft_gate():
    """The loop guard still releases the SOFT (observation) gate: with a valid spec
    present the evidence gate passes, and stop_hook_active=True does not block."""
    with tempfile.TemporaryDirectory() as cwd:
        save_spec(cwd, "st5b", _standard_spec_with_evidence())
        out = run_stop({"session_id": "st5b", "cwd": cwd, "stop_hook_active": True}, grade="STANDARD")
        assert not _blocks(out)


def test_stop_no_spec_blocks_infinitely():
    """No cap: with no spec the evidence gate blocks every stop (no release after N)."""
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        p = {"session_id": "st6", "cwd": cwd, "stop_hook_active": False}
        results = [_blocks(run_stop(p, grade="STANDARD", data_dir=dd)) for _ in range(4)]
        assert all(results), results


def test_stop_valid_spec_releases():
    """The infinite block clears the moment a valid spec exists."""
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        p = {"session_id": "st6b", "cwd": cwd, "stop_hook_active": False}
        assert _blocks(run_stop(p, grade="STANDARD", data_dir=dd))
        save_spec(cwd, "st6b", _standard_spec_with_evidence())
        assert not _blocks(run_stop(p, grade="STANDARD", data_dir=dd))


def test_stop_escape_hatch_removed():
    """The escape hatch is removed at completion too: UNIFABLE_EVIDENCE_GATE=0 no
    longer disables the stop gate. No spec still blocks."""
    with tempfile.TemporaryDirectory() as cwd:
        out = run_stop(
            {"session_id": "st7", "cwd": cwd, "stop_hook_active": False},
            evidence_gate="0", grade="STANDARD",
        )
        assert _blocks(out)


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
