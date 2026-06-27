#!/usr/bin/env python3
"""Tests for spec.py (validate_spec, check_fake_evidence) and
pre_tool_use.py (PROTECTED_PATHS guard, spec gate allow/block).

Invokes pre_tool_use.py via subprocess with crafted stdin, matching the
pattern used by test_gate.py for the other hooks.
"""

from __future__ import annotations

import contextlib
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
    append_frontier_task,
    check_fake_evidence,
    format_spec_validation_block,
    is_path_line,
    is_source_url,
    save_spec,
    set_primary_task,
    spec_path,
    spec_template,
    validate_spec,
)


def _heavy_spec_with_approaches(**overrides) -> dict:
    spec = {
        "restated_goal": "Rewrite auth to use JWT.",
        "acceptance_criteria": [
            {"check": "pytest tests/test_jwt.py", "evidence": "3 passed in 0.2s"},
        ],
        "heavy_workflow": True,
        "tasks": [],
        "repo_context": [{"cite": "src/auth.py:30", "why": "auth entrypoint being rewritten"}],
        "prior_art": [{"cite": "https://datatracker.ietf.org/doc/html/rfc7519", "why": "JWT claims spec"}],
    }
    append_frontier_task(spec, "Session cookies hardened", "pytest tests/test_sess.py")
    append_frontier_task(spec, "Rotating JWT keys", "pytest tests/test_jwt_rot.py")
    set_primary_task(spec, "HMAC bearer tokens", "pytest tests/test_hmac.py")
    spec.update(overrides)
    return spec


sys.path.insert(0, str(HOOKS))
import gate_stop  # noqa: E402
import pre_tool_use  # noqa: E402

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
    # These tests prove the FORMAT evidence gate; citation truth-checking (activity
    # backs the citations) is covered in tests/test_citation_verify.py.
    env["UNIFABLE_VERIFY_CITATIONS"] = "0"
    # These tests assert the deterministic evidence gate, not the breaker/director
    # judge. Make the judge hermetically offline so the result does not depend on
    # whether the dev machine has live Realtime credentials (with a reachable judge
    # the director would add additionalContext on the allow path). Callers that test
    # the judge can re-enable it via env_extra.
    env["UNIFABLE_JUDGE_OFFLINE"] = "1"
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
        "acceptance_criteria": [{"check": "python cli.py --verbose 2>&1 | grep verbose", "evidence": "verbose mode enabled"}],
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
        "acceptance_criteria": [{"check": "pytest tests/test_rate_limit.py -v", "evidence": "5 passed in 0.4s"}],
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
        "acceptance_criteria": [{"check": "pytest tests/test_auth.py", "evidence": "tbd"}],
    }
    ok, reasons = validate_spec(spec, "STANDARD")
    assert not ok
    assert any("placeholder" in r.lower() or "tbd" in r.lower() for r in reasons)


def test_validate_heavy_passes():
    ok, reasons = validate_spec(_heavy_spec_with_approaches(), "HEAVY", require_evidence=True)
    assert ok, reasons


def test_validate_heavy_missing_frontiers():
    spec = _heavy_spec_with_approaches()
    spec["tasks"] = [t for t in spec["tasks"] if t.get("approach_kind") != "frontier"]
    ok, reasons = validate_spec(spec, "HEAVY", require_evidence=True)
    assert not ok
    assert any("frontier" in r for r in reasons)


def test_validate_heavy_missing_primary():
    spec = _heavy_spec_with_approaches()
    spec["tasks"] = [t for t in spec["tasks"] if t.get("approach_kind") != "primary"]
    ok, reasons = validate_spec(spec, "HEAVY", require_evidence=True)
    assert not ok
    assert any("primary" in r for r in reasons)


def test_validate_unknown_grade():
    spec = spec_template()
    ok, reasons = validate_spec(spec, "EXTREME")
    assert not ok
    assert any("Unknown grade" in r for r in reasons)


def test_frontier_judge_schema_accepts_three_outcomes():
    """The frontier judge schema must accept accepted_approach in addition to
    rejected_approach and still_viable."""
    # Verify the enum includes all three outcomes
    from spec_judge import _FRONTIER_JUDGE_SCHEMA

    outcome_prop = _FRONTIER_JUDGE_SCHEMA["properties"]["outcome"]
    assert "accepted_approach" in outcome_prop["enum"]
    assert "rejected_approach" in outcome_prop["enum"]
    assert "still_viable" in outcome_prop["enum"]


# ---------------------------------------------------------------------------
# Citation-format helpers
# ---------------------------------------------------------------------------


def test_is_path_line():
    assert is_path_line("src/app.py:42")
    assert is_path_line("a/b/c.py:10-20")
    assert is_path_line("hooks/gate_stop.py:5")
    assert not is_path_line("src/app.py")  # no line number
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
        "acceptance_criteria": [{"check": "pytest tests/test_rate_limit.py -v", "evidence": "5 passed in 0.4s"}],
        "repo_context": [
            {"cite": "src/middleware.py:88", "why": "rate-limit hook attaches here"},
            {"cite": "src/router.py:12-20", "why": "endpoint registration the middleware wraps"},
        ],
        "prior_art": [
            {"cite": "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429", "why": "429 retry semantics for the limiter"}
        ],
    }


def test_evidence_off_is_backward_compatible():
    """require_evidence defaults False: a spec with no repo_context still passes."""
    spec = {
        "restated_goal": "Add a flag.",
        "acceptance_criteria": [{"check": "echo ok", "evidence": "ok"}],
    }
    ok, reasons = validate_spec(spec, "STANDARD")
    assert ok, reasons
    ok2, _ = validate_spec(spec, "STANDARD", require_evidence=False)
    assert ok2


def test_evidence_standard_requires_repo_context():
    spec = {
        "restated_goal": "Add a flag.",
        "acceptance_criteria": [{"check": "echo ok", "evidence": "ok"}],
    }
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("repo_context" in r for r in reasons)


def test_evidence_standard_passes_with_repo_context():
    ok, reasons = validate_spec(_standard_spec_with_evidence(), "STANDARD", require_evidence=True)
    assert ok, reasons


def test_evidence_repo_context_malformed_blocks():
    spec = _standard_spec_with_evidence()
    spec["repo_context"] = [{"cite": "src/middleware.py", "why": "the hook"}]  # missing :line
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("repo_context" in r and "path:line" in r for r in reasons)


def test_evidence_repo_context_placeholder_blocks():
    spec = _standard_spec_with_evidence()
    spec["repo_context"] = [{"cite": "src/app.py:1", "why": "tbd"}]
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("repo_context" in r for r in reasons)


def test_evidence_repo_context_requires_why():
    """A repo_context citation with no 'why' rationale is rejected."""
    spec = _standard_spec_with_evidence()
    spec["repo_context"] = [{"cite": "src/app.py:42", "why": ""}]
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("repo_context" in r and "why" in r for r in reasons)


def test_evidence_standard_requires_prior_art():
    """prior_art (source URL) is required from STANDARD up, not only HEAVY."""
    spec = _standard_spec_with_evidence()
    spec.pop("prior_art", None)
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("prior_art" in r for r in reasons)


def test_repo_maintenance_waives_prior_art():
    """Version bump / manifest sync needs repo_context only, not external prior_art."""
    spec = _standard_spec_with_evidence()
    spec["restated_goal"] = "Patch bump plugin version with just version and sync manifests"
    spec.pop("prior_art", None)
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert ok, reasons
    assert not any("prior_art" in r for r in reasons)


def test_in_repo_regression_test_waives_prior_art():
    """Regression tests bounded to this repo need repo_context only."""
    spec = _standard_spec_with_evidence()
    spec["restated_goal"] = (
        "Add a focused regression test proving saved summaries require all four benchmark cells"
    )
    spec["tasks"] = [
        {
            "id": "T1",
            "title": "Regression test for four-cell acceptance",
            "check": "python3 -m pytest tests/test_benchmark_harness.py::test_saved_summary -q",
            "status": "pending",
        }
    ]
    spec.pop("prior_art", None)
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert ok, reasons
    assert not any("prior_art" in r for r in reasons)


def test_external_research_overrides_in_repo_waiver():
    """External-research signals still require prior_art even when adding tests."""
    spec = _standard_spec_with_evidence()
    spec["restated_goal"] = "Add regression test for third-party API platform behavior"
    spec.pop("prior_art", None)
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("prior_art" in r for r in reasons)


def test_normal_code_still_requires_prior_art():
    """Non-maintenance code tasks still require prior_art."""
    spec = _standard_spec_with_evidence()
    spec["restated_goal"] = "Add OAuth token refresh to the API client"
    spec.pop("prior_art", None)
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("prior_art" in r for r in reasons)


def test_format_spec_validation_block_prior_art_actionable():
    """Missing prior_art should tell the model to fetch, not expose spec paths."""
    _, reasons = validate_spec(
        {k: v for k, v in _standard_spec_with_evidence().items() if k != "prior_art"},
        "STANDARD",
        require_evidence=True,
    )
    msg = format_spec_validation_block("STANDARD", reasons)
    assert "prior_art" in msg
    assert "WebFetch" in msg or "fetch" in msg.lower()
    assert "spec.json" not in msg
    assert "/.unifable/specs/" not in msg


def test_standard_missing_prior_art_block_message():
    """PreToolUse block omits spec path and includes fetch guidance."""
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "missing-prior-art"
        spec = _standard_spec_with_evidence()
        spec.pop("prior_art", None)
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(
            os.path.join(cwd, "src", "main.py"),
            session_id=session_id,
            cwd=cwd,
        )
        rc, _, stderr = run_pre_tool(payload, spec_gate="1", grade="STANDARD")
        assert rc == 2
        assert "spec at " not in stderr
        assert "prior_art" in stderr
        assert "fetch" in stderr.lower()


def test_evidence_light_exempt():
    """LIGHT is exempt from citation requirements even when require_evidence=True."""
    spec = {
        "restated_goal": "Fix a typo.",
        "acceptance_criteria": [{"check": "echo ok", "evidence": "ok"}],
    }
    ok, reasons = validate_spec(spec, "LIGHT", require_evidence=True)
    assert ok, reasons


def test_evidence_heavy_requires_prior_art():
    spec = _heavy_spec_with_approaches()
    spec.pop("prior_art", None)
    ok, reasons = validate_spec(spec, "HEAVY", require_evidence=True)
    assert not ok
    assert any("prior_art" in r for r in reasons)


def test_evidence_heavy_passes_with_prior_art():
    ok, reasons = validate_spec(_heavy_spec_with_approaches(), "HEAVY", require_evidence=True)
    assert ok, reasons


def test_evidence_heavy_prior_art_must_be_url():
    spec = _heavy_spec_with_approaches()
    spec["prior_art"] = [{"cite": "some blog I read", "why": "background"}]
    ok, reasons = validate_spec(spec, "HEAVY", require_evidence=True)
    assert not ok
    assert any("prior_art" in r and "URL" in r for r in reasons)


def test_evidence_prior_art_requires_why():
    """A bare prior_art URL (no 'why') is rejected, mirroring repo_context."""
    spec = _standard_spec_with_evidence()
    spec["prior_art"] = [{"cite": "https://example.com/doc", "why": ""}]
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("prior_art" in r and "why" in r for r in reasons), reasons


def _operational_spec_with_tasks() -> dict:
    return {
        "restated_goal": "Research NRG account across internal systems and draft a reply.",
        "evidence_profile": "operational",
        "requires_tasks": True,
        "tasks": [
            {
                "id": "T1",
                "title": "Resolve NRG account facts from Salesforce",
                "check": "echo nrg facts gathered",
                "status": "pending",
            }
        ],
    }


def test_operational_standard_waives_citations():
    spec = _operational_spec_with_tasks()
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert ok, reasons


def test_code_standard_still_requires_both():
    spec = _standard_spec_with_evidence()
    spec.pop("prior_art", None)
    ok, reasons = validate_spec(spec, "STANDARD", require_evidence=True)
    assert not ok
    assert any("prior_art" in r for r in reasons)


def test_operational_pre_tool_unlocks_write():
    with tempfile.TemporaryDirectory() as tmp_root, tempfile.TemporaryDirectory() as cwd:
        session_id = "operational-write"
        os.environ["UNIFABLE_DATA"] = tmp_root
        try:
            spec = _operational_spec_with_tasks()
            save_spec(cwd, session_id, spec)
            target = os.path.join(cwd, "scratchpad", "nrg-research.md")
            payload = _edit_payload(target, session_id=session_id, cwd=cwd)
            payload["tool_name"] = "Write"
            payload["tool_input"] = {"file_path": target, "contents": "# NRG research\n"}
            rc, _, stderr = run_pre_tool(payload, spec_gate="1", grade="STANDARD", tmp_root=tmp_root)
            assert rc == 0, stderr
        finally:
            os.environ.pop("UNIFABLE_DATA", None)


def test_operational_stop_skips_citation_requirements():
    with tempfile.TemporaryDirectory() as cwd:
        spec = {
            "restated_goal": "Research NRG account and draft a reply to Bill.",
            "evidence_profile": "operational",
            "acceptance_criteria": [{"check": "echo ok", "evidence": "ok"}],
        }
        save_spec(cwd, "st-operational", spec)
        out = run_stop(
            {"session_id": "st-operational", "cwd": cwd, "stop_hook_active": False},
            grade="STANDARD",
        )
        assert not _blocks(out), out


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


def _delegate_payload(tool_name: str = "Task", session_id: str = "sess-abc123", cwd: str = "/work") -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"description": "inspect auth flow", "prompt": "Read only and report findings."},
        "session_id": session_id,
        "cwd": cwd,
    }


# --- Removed escape hatch: env no longer disables the gate ---


def test_disable_env_has_no_effect():
    """The escape hatch is removed: setting UNIFABLE_EVIDENCE_GATE=0 / SPEC_GATE=0
    does NOT disable the gate. A STANDARD edit with no spec is still blocked."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(os.path.join(cwd, "src", "main.py"), session_id="disable-noop", cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, spec_gate="0", evidence_gate="0", grade="STANDARD")
        assert rc == 2
        assert "spec" in stderr.lower()


def test_whitelisted_bash_passes_without_spec():
    """A whitelisted Bash command is allowed even with no spec."""
    rc, out, _ = run_pre_tool(_bash_payload("rg --files"), grade="STANDARD")
    assert rc == 0


def test_non_whitelisted_bash_blocks_without_spec():
    with tempfile.TemporaryDirectory() as data_root:
        rc, out, stderr = run_pre_tool(
            _bash_payload("cat README.md", session_id="bash-block-test"),
            grade="STANDARD",
            tmp_root=data_root,
        )
        assert rc == 2
        assert "Allowed now:" in stderr or "Allowed before unlock" in stderr
        assert "cd, ls, glob, rg" in stderr
        assert "unifable restate" in stderr or "append-only" in stderr


def test_task_agent_block_without_spec():
    with tempfile.TemporaryDirectory() as data_root:
        for tool_name in ("Task", "Agent"):
            rc, out, stderr = run_pre_tool(
                _delegate_payload(tool_name, session_id=f"delegate-{tool_name}"),
                grade="STANDARD",
                tmp_root=data_root,
            )
            assert rc == 2
            assert (
                "unifable restate" in stderr
                or "Allowed now:" in stderr
                or "Evidence spec required" in stderr
                or stderr.strip() == ""
            )


def test_task_agent_light_waived():
    for tool_name in ("Task", "Agent"):
        rc, out, _ = run_pre_tool(_delegate_payload(tool_name), grade="LIGHT")
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


# --- apply_patch protected-path guard (all hosts, not just Claude file_path) ---


def _apply_patch_payload(patch: str, session_id: str = "sess-abc123", cwd: str = "/work") -> dict:
    return {
        "tool_name": "apply_patch",
        "tool_input": {"patch": patch},
        "tool_response": {"success": True},
        "session_id": session_id,
        "cwd": cwd,
    }


def test_apply_patch_global_spec_blocked():
    """A Codex-shape apply_patch rewriting the global keyed spec.json is blocked.

    This matches how an agent can hand-edit the spec to fake validated statuses: the patch target lived in the envelope text, not in a
    `file_path` key, so the old _target_path substring check missed it."""
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        session_id = "apply-patch-spec"
        with _data_env(dd):
            abs_spec = str(spec_path(cwd, session_id))
        patch = (
            f'*** Begin Patch\n*** Update File: {abs_spec}\n@@\n-  "status": "pending"\n+  "status": "validated"\n*** End Patch\n'
        )
        payload = _apply_patch_payload(patch, session_id=session_id, cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, spec_gate="0", tmp_root=dd)
        assert rc == 2
        low = stderr.lower()
        assert "protected" in low or "spec" in low or "cli-only" in low or "cli only" in low


def test_apply_patch_repo_local_spec_blocked():
    """apply_patch targeting a repo-local .unifable/spec/<session>.json is blocked."""
    with tempfile.TemporaryDirectory() as cwd:
        abs_spec = os.path.join(cwd, ".unifable", "spec", "sess-abc123.json")
        patch = f"*** Begin Patch\n*** Update File: {abs_spec}\n@@\n-old\n+new\n*** End Patch\n"
        payload = _apply_patch_payload(patch, cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, spec_gate="0")
        assert rc == 2
        low = stderr.lower()
        assert "protected" in low or "spec" in low or "cli-only" in low or "cli only" in low


def test_apply_patch_normal_source_file_allowed():
    """apply_patch to a normal repo source file is NOT blocked by the protected guard.

    LIGHT waives the spec requirement, so a non-protected target reaches allow."""
    with tempfile.TemporaryDirectory() as cwd:
        abs_src = os.path.join(cwd, "lib", "foo.py")
        patch = f"*** Begin Patch\n*** Update File: {abs_src}\n@@\n-x = 1\n+x = 2\n*** End Patch\n"
        payload = _apply_patch_payload(patch, cwd=cwd)
        rc, _, _ = run_pre_tool(payload, grade="LIGHT")
        assert rc == 0


def test_apply_patch_targets_unit_codex_and_git_shapes():
    """_apply_patch_targets extracts paths from both envelope shapes and across keys."""
    import protected_paths

    codex = (
        "*** Begin Patch\n"
        "*** Update File: a/spec.json\n"
        "*** Add File: b/new.py\n"
        "*** Delete File: c/old.py\n"
        "*** Move to: d/moved.py\n"
        "*** End Patch\n"
    )
    assert protected_paths._apply_patch_targets({"patch": codex}) == [
        "a/spec.json",
        "b/new.py",
        "c/old.py",
        "d/moved.py",
    ]
    git = "--- a/src/old.py\n+++ b/src/new.py\n@@\n-1\n+2\n"
    git_targets = protected_paths._apply_patch_targets({"content": git})
    assert "src/old.py" in git_targets and "src/new.py" in git_targets
    # /dev/null is dropped (git add/delete sentinel)
    devnull = "--- /dev/null\n+++ b/created.py\n"
    assert protected_paths._apply_patch_targets({"x": devnull}) == ["created.py"]
    # Unknown key still works: every string value is scanned.
    assert protected_paths._apply_patch_targets({"weird_key": codex})[0] == "a/spec.json"
    # tool_input itself a string is handled.
    assert protected_paths._apply_patch_targets("*** Update File: z.py\n") == ["z.py"]
    # Junk never raises.
    assert protected_paths._apply_patch_targets({"n": 5, "ok": None}) == []


# --- Bash protected-write guard (action phase allows all shell otherwise) ---


def test_bash_redirect_into_spec_blocked():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        session_id = "bash-redirect-spec"
        with _data_env(dd):
            abs_spec = str(spec_path(cwd, session_id))
        payload = _bash_payload(f"echo zzz > {abs_spec}", session_id=session_id, cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, grade="STANDARD", tmp_root=dd)
        assert rc == 2
        low = stderr.lower()
        assert "protected" in low or "cli-only" in low or "cli only" in low


def test_bash_rm_spec_blocked():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        session_id = "bash-rm-spec"
        with _data_env(dd):
            abs_spec = str(spec_path(cwd, session_id))
        payload = _bash_payload(f"rm {abs_spec}", session_id=session_id, cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, grade="STANDARD", tmp_root=dd)
        assert rc == 2
        low = stderr.lower()
        assert "protected" in low or "cli-only" in low or "cli only" in low


def test_bash_sed_inplace_spec_blocked():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        session_id = "bash-sed-spec"
        with _data_env(dd):
            abs_spec = str(spec_path(cwd, session_id))
        payload = _bash_payload(f"sed -i s/a/b/ {abs_spec}", session_id=session_id, cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, grade="STANDARD", tmp_root=dd)
        assert rc == 2
        low = stderr.lower()
        assert "protected" in low or "cli-only" in low or "cli only" in low


def test_bash_read_of_spec_not_blocked_by_protected_guard():
    """A non-mutating read of a protected path returns None from the dedicated
    guard. (The full hook may still block it via the research whitelist, so we
    unit-test the guard directly rather than driving the subprocess.)"""
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        with _data_env(dd):
            abs_spec = str(spec_path(cwd, "bash-read-spec"))
            assert pre_tool_use._bash_protected_write(f"cat {abs_spec}", cwd) is None
            assert pre_tool_use._bash_protected_write(f"rg foo {abs_spec}", cwd) is None
            # A mutating command targeting the spec IS caught.
            assert pre_tool_use._bash_protected_write(f"rm {abs_spec}", cwd) == abs_spec
            # A non-protected mutation is not caught.
            assert pre_tool_use._bash_protected_write(f"rm {os.path.join(cwd, 'lib', 'foo.py')}", cwd) is None


def test_bash_protected_write_tilde_path():
    """Guard (a): a literal ~/.unifable/specs/... mutation resolves and is caught."""
    # data_root defaults to ~/.unifable when UNIFABLE_DATA is unset, so a literal
    # ~/.unifable/specs/... path expands into the protected store.
    old = os.environ.pop("UNIFABLE_DATA", None)
    try:
        token = "~/.unifable/specs/abc/sess/spec.json"
        hit = pre_tool_use._bash_protected_write(f"rm {token}", "/work")
        assert hit == token
    finally:
        if old is not None:
            os.environ["UNIFABLE_DATA"] = old


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
            "repo_context": [{"cite": "src/server.py:12", "why": "where routes register"}],
            "prior_art": [{"cite": "https://datatracker.ietf.org/doc/html/rfc9110", "why": "HTTP semantics for the endpoint"}],
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
            "acceptance_criteria": [{"check": "pytest tests/test_auth.py", "evidence": "tbd"}],
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


# --- Spec gate: HEAVY frontier-first workflow ---


def test_heavy_missing_frontiers_blocks():
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "heavy-session-001"
        spec = {
            "restated_goal": "Migrate DB schema.",
            "acceptance_criteria": [
                {"check": "python manage.py test", "evidence": "1 passed"},
            ],
            "heavy_workflow": True,
            "tasks": [],
            "repo_context": [{"cite": "migrations/0001.py:1", "why": "prior migration"}],
            "prior_art": [{"cite": "https://example.com/migrations", "why": "reference"}],
        }
        set_primary_task(spec, "Primary migration path", "python manage.py test")
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(
            os.path.join(cwd, "migrations", "0002.py"),
            session_id=session_id,
            cwd=cwd,
        )
        rc, _, stderr = run_pre_tool(payload, spec_gate="1", grade="HEAVY")
        assert rc == 2
        assert "frontier" in stderr.lower()


def test_heavy_valid_spec_allows():
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "heavy-session-002"
        spec = _heavy_spec_with_approaches(
            restated_goal="Migrate DB schema to add verified_at.",
            acceptance_criteria=[
                {"check": "python manage.py test", "evidence": "1 passed in 0.9s"},
            ],
            repo_context=[{"cite": "migrations/0001.py:1", "why": "prior migration this extends"}],
            prior_art=[{"cite": "https://docs.djangoproject.com/en/stable/topics/migrations/", "why": "reference"}],
        )
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(
            os.path.join(cwd, "migrations", "0002.py"),
            session_id=session_id,
            cwd=cwd,
        )
        rc, out, _ = run_pre_tool(payload, spec_gate="1", grade="HEAVY")
        assert rc == 0


# --- Non-write tool is never blocked ---


def test_bash_not_blocked_after_valid_spec():
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "bash-unlocked"
        save_spec(cwd, session_id, _standard_spec_with_evidence())
        payload = _bash_payload("echo test", session_id=session_id, cwd=cwd)
        rc, out, _ = run_pre_tool(payload, spec_gate="1", grade="STANDARD")
        assert rc == 0


def test_task_agent_not_blocked_after_valid_spec():
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "delegate-unlocked"
        save_spec(cwd, session_id, _standard_spec_with_evidence())
        for tool_name in ("Task", "Agent"):
            payload = _delegate_payload(tool_name, session_id=session_id, cwd=cwd)
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
            session_id="brick-sess",
            cwd=cwd,
        )
        rc, _, stderr = run_pre_tool(
            payload,
            spec_gate="0",
            grade="STANDARD",
            env_extra={"UNIFABLE_EVIDENCE_GATE": "1"},
        )
        assert rc == 2, "direct spec authoring must be blocked (CLI-only)"
        assert "spec.py" in stderr.lower() or "protected" in stderr.lower()


def test_evidence_gate_default_on_blocks_uncited_edit():
    """Production default (no gate env set): an uncited edit on a STANDARD task is
    blocked. Proves the gate is ON by default."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(os.path.join(cwd, "src", "main.py"), session_id="default-on-sess", cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, spec_gate=None, evidence_gate=None, grade="STANDARD")
        assert rc == 2, "evidence gate must block uncited edits by default"
        assert "repo_context" in stderr or "spec" in stderr.lower()


def test_evidence_gate_escape_hatch_removed():
    """The escape hatch is removed: UNIFABLE_EVIDENCE_GATE=0 no longer disables the
    gate. An uncited STANDARD edit is still blocked."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(os.path.join(cwd, "src", "main.py"), session_id="escape-sess", cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, spec_gate=None, evidence_gate="0", grade="STANDARD")
        assert rc == 2
        assert "spec" in stderr.lower()


def test_evidence_gate_default_on_light_waived():
    """Default-on still waives LIGHT (quick) tasks — trivial edits are not over-gated."""
    with tempfile.TemporaryDirectory() as cwd:
        payload = _edit_payload(os.path.join(cwd, "src", "main.py"), session_id="light-sess", cwd=cwd)
        rc, out, _ = run_pre_tool(payload, spec_gate=None, evidence_gate=None, grade="LIGHT")
        assert rc == 0


def test_evidence_gate_blocks_valid_spec_without_repo_context():
    """A spec that passes the spec gate is still blocked by the evidence gate when
    it lacks repo_context citations."""
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "ev-session-001"
        spec = {
            "restated_goal": "Add health-check endpoint.",
            "acceptance_criteria": [{"check": "curl -s localhost:8000/health", "evidence": '"ok"'}],
        }
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(os.path.join(cwd, "src", "server.py"), session_id=session_id, cwd=cwd)
        rc, _, stderr = run_pre_tool(
            payload,
            spec_gate="0",
            grade="STANDARD",
            env_extra={"UNIFABLE_EVIDENCE_GATE": "1"},
        )
        assert rc == 2
        assert "repo_context" in stderr


def test_evidence_gate_allows_spec_with_citations():
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "ev-session-002"
        spec = {
            "restated_goal": "Add health-check endpoint.",
            "acceptance_criteria": [{"check": "curl -s localhost:8000/health", "evidence": '"ok"'}],
            "repo_context": [
                {"cite": "src/server.py:10", "why": "app factory where routes mount"},
                {"cite": "src/routes.py:5-8", "why": "route table the endpoint joins"},
            ],
            "prior_art": [{"cite": "https://datatracker.ietf.org/doc/html/rfc9110", "why": "HTTP semantics for the endpoint"}],
        }
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(os.path.join(cwd, "src", "server.py"), session_id=session_id, cwd=cwd)
        rc, out, _ = run_pre_tool(
            payload,
            spec_gate="0",
            grade="STANDARD",
            env_extra={"UNIFABLE_EVIDENCE_GATE": "1"},
        )
        assert rc == 0
        assert out == {}


def test_spec_only_env_does_not_downgrade():
    """The spec-only mode is removed: UNIFABLE_SPEC_GATE=1 no longer downgrades the
    gate. A citationless spec is still rejected (repo_context required)."""
    with tempfile.TemporaryDirectory() as cwd:
        session_id = "ev-session-003"
        spec = {
            "restated_goal": "Add health-check endpoint.",
            "acceptance_criteria": [{"check": "curl -s localhost:8000/health", "evidence": '"ok"'}],
        }
        save_spec(cwd, session_id, spec)
        payload = _edit_payload(os.path.join(cwd, "src", "server.py"), session_id=session_id, cwd=cwd)
        rc, _, stderr = run_pre_tool(payload, spec_gate="1", grade="STANDARD")
        assert rc == 2
        assert "repo_context" in stderr


def test_empty_stdin_fails_open():
    """Empty / malformed JSON must not crash the hook."""
    env = dict(os.environ)
    env["UNIFABLE_SPEC_GATE"] = "1"
    env["UNIFABLE_GRADE"] = "STANDARD"
    env["UNIFABLE_JUDGE_OFFLINE"] = "1"  # hermetic: no live breaker/director judge
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
    # Citation truth-checking has its own suite (tests/test_citation_verify.py);
    # the Stop-gate tests here isolate the format/breaker behavior.
    env["UNIFABLE_VERIFY_CITATIONS"] = "0"
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


def _write_transcript(path: Path, content: list[dict]) -> None:
    path.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}}) + "\n")


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
        assert "prior_art" in out.get("reason", "").lower()


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


def test_stop_handoff_blocks_deferred_work(monkeypatch, tmp_path):
    """Completion handoff judge blocks when the agent defers autonomous work."""
    import completion_handoff as ch

    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [{"type": "text", "text": "I'll now implement the fix and run tests."}],
    )

    def fake_judge(*_a, **_k):
        return {
            "ok_to_stop": False,
            "reason": "Promised work without acting.",
            "steering": "Implement the fix now.",
        }

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("UNIFABLE_DATA", str(data_dir))
    monkeypatch.setattr(ch, "judge_completion_handoff", fake_judge)
    out = ch.completion_handoff_decision(
        {
            "session_id": "st5c",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        },
        tmp_path,
    )
    assert out and out.get("decision") == "block"
    assert "Stop blocked: finish the pending work now." in out.get("reason", "")


def test_stop_handoff_allows_genuine_user_choice(monkeypatch, tmp_path):
    import completion_handoff as ch

    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [{"type": "text", "text": "I'll implement that next. Would you like option A or B?"}],
    )

    def fake_judge(*_a, **_k):
        return {"ok_to_stop": True, "reason": "User-owned choice.", "steering": "", "blocked_on_user_only": True}

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("UNIFABLE_DATA", str(data_dir))
    monkeypatch.setattr(ch, "judge_completion_handoff", fake_judge)
    out = ch.completion_handoff_decision(
        {
            "session_id": "st5d",
            "cwd": str(tmp_path),
            "transcript_path": str(transcript),
            "stop_hook_active": False,
        },
        tmp_path,
    )
    assert out is None


def test_stop_handoff_allows_tool_use_and_blocks_despite_loop_guard(monkeypatch, tmp_path):
    import completion_handoff as ch

    transcript = tmp_path / "session.jsonl"
    _write_transcript(
        transcript,
        [
            {"type": "text", "text": "I'll now run the check."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}},
        ],
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("UNIFABLE_DATA", str(data_dir))
    payload = {
        "session_id": "st5e",
        "cwd": str(tmp_path),
        "transcript_path": str(transcript),
        "stop_hook_active": False,
    }
    assert ch.completion_handoff_decision(payload, tmp_path) is None

    _write_transcript(
        transcript,
        [{"type": "text", "text": "I'll now implement the fix and run tests."}],
    )
    payload["stop_hook_active"] = True

    def fake_judge(*_a, **_k):
        return {
            "ok_to_stop": False,
            "reason": "Deferred work.",
            "steering": "Implement now.",
        }

    monkeypatch.setattr(ch, "judge_completion_handoff", fake_judge)
    out = ch.completion_handoff_decision(payload, tmp_path)
    assert out and out.get("decision") == "block"


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
        with _data_env(dd):
            save_spec(cwd, "st6b", _standard_spec_with_evidence())
        assert not _blocks(run_stop(p, grade="STANDARD", data_dir=dd))


def test_stop_escape_hatch_removed():
    """The escape hatch is removed at completion too: UNIFABLE_EVIDENCE_GATE=0 no
    longer disables the stop gate. No spec still blocks."""
    with tempfile.TemporaryDirectory() as cwd:
        out = run_stop(
            {"session_id": "st7", "cwd": cwd, "stop_hook_active": False},
            evidence_gate="0",
            grade="STANDARD",
        )
        assert _blocks(out)


@contextlib.contextmanager
def _data_env(dd: str):
    """Point UNIFABLE_DATA at *dd* in-process (so save_spec / keyed-path helpers
    land where the subprocess gate reads), restoring the prior value after."""
    old = os.environ.get("UNIFABLE_DATA")
    os.environ["UNIFABLE_DATA"] = dd
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("UNIFABLE_DATA", None)
        else:
            os.environ["UNIFABLE_DATA"] = old


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
