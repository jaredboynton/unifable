#!/usr/bin/env python3
"""Regression test: the PreToolUse spec gate must route Bash into pre_tool_use.py
on BOTH host manifests, and must block a non-whitelisted Bash command even under
permission_mode=bypassPermissions (YOLO / full-auto).

Why this exists: the bash research-whitelist lockdown lives in
hooks/pre_tool_use.py (`_enforce_bash`, dispatched at the `tool_name == "Bash"`
branch). It is dead code on a host unless that host's PreToolUse matcher actually
selects `Bash`. The behavioral tests in test_spec_gate.py invoke the hook script
directly, so they pass even if the manifest never wires Bash to it. This file
guards the wiring itself, on both manifests, plus the permission-mode
independence that makes the deny survive YOLO.

Grounding for the YOLO claim is in the Codex source: a PreToolUse `Blocked`
outcome returns an error to the model before the tool handler runs, with no
permission-mode check (codex-rs/core/src/tools/registry.rs:505) and the hook is
still dispatched in bypass mode (codex-rs/core/src/hook_runtime.rs maps
AskForApproval::Never -> "bypassPermissions"). The hook therefore vetoes the tool
regardless of approval policy. See https://developers.openai.com/codex/hooks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
PY = sys.executable

# The two host manifests that wire PreToolUse -> pre_tool_use.py.
MANIFESTS = {
    "claude": REPO / "hooks" / "hooks.json",
    "codex": REPO / ".codex-plugin" / "hooks.json",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pre_tool_matcher(manifest_path: Path) -> str:
    """Return the PreToolUse matcher whose hook command runs pre_tool_use.py."""
    data = json.loads(manifest_path.read_text())
    groups = data["hooks"]["PreToolUse"]
    for grp in groups:
        cmds = " ".join(h.get("command", "") for h in grp.get("hooks", []))
        if "pre_tool_use.py" in cmds:
            return grp["matcher"]
    raise AssertionError(f"{manifest_path}: no PreToolUse hook runs pre_tool_use.py")


def _post_tool_matcher(manifest_path: Path, script: str) -> str | None:
    data = json.loads(manifest_path.read_text())
    for grp in data["hooks"].get("PostToolUse", []):
        cmds = " ".join(h.get("command", "") for h in grp.get("hooks", []))
        if script in cmds:
            return grp["matcher"]
    return None


def run_pre_tool_bash(
    command: str,
    *,
    grade: str = "STANDARD",
    permission_mode: str = "bypassPermissions",
) -> tuple[int, str]:
    """Drive hooks/pre_tool_use.py with a Bash payload and return (rc, stderr).

    Runs against a fresh tempdir cwd/data so no spec exists (research phase) and
    no prior ledger state leaks in. The breaker and citation truth-check are
    disabled so the test is offline and deterministic; this exercises the
    research-whitelist branch only.
    """
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": "bash-gate-test",
            "cwd": tmp,
            "permission_mode": permission_mode,
        }
        env = dict(os.environ)
        env["UNIFABLE_GRADE"] = grade
        env["UNIFABLE_BREAKER"] = "0"
        env["UNIFABLE_VERIFY_CITATIONS"] = "0"
        env["UNIFABLE_DATA"] = tmp
        proc = subprocess.run(
            [PY, str(HOOKS / "pre_tool_use.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )
        return proc.returncode, proc.stderr


# ---------------------------------------------------------------------------
# Manifest wiring: the actual regression guard for the matcher widen
# ---------------------------------------------------------------------------

def test_both_manifests_route_bash_to_pre_tool_use():
    """Both host PreToolUse matchers must select Bash, or the lockdown never
    fires on that host."""
    for host, path in MANIFESTS.items():
        matcher = _pre_tool_matcher(path)
        assert re.match(matcher, "Bash"), f"{host}: matcher {matcher!r} does not match 'Bash'"


def test_both_manifests_still_route_edit_and_apply_patch():
    """Widening for Bash must not have dropped the original edit/apply_patch
    coverage."""
    for host, path in MANIFESTS.items():
        matcher = _pre_tool_matcher(path)
        for tool in ("Edit", "Write", "apply_patch"):
            assert re.match(matcher, tool), f"{host}: matcher {matcher!r} dropped {tool!r}"


def test_test_after_edit_does_not_match_bash():
    """The PostToolUse test-after-edit runner must NOT fire on Bash -- only the
    spec gate should. Guards against widening the wrong hook."""
    for host, path in MANIFESTS.items():
        matcher = _post_tool_matcher(path, "test_after_edit.py")
        if matcher is None:
            continue  # host does not wire test_after_edit
        assert not re.match(matcher, "Bash"), f"{host}: test_after_edit wrongly matches 'Bash'"


# ---------------------------------------------------------------------------
# Behavior: the deny survives YOLO and is permission-mode independent
# ---------------------------------------------------------------------------

def test_non_whitelisted_bash_blocked_under_bypass_permissions():
    """A non-whitelisted Bash command is blocked (exit 2) even when the host
    reports permission_mode=bypassPermissions (YOLO)."""
    rc, stderr = run_pre_tool_bash("echo hi", permission_mode="bypassPermissions")
    assert rc == 2, f"expected block (rc 2), got {rc}; stderr={stderr!r}"
    assert "whitelist" in stderr.lower() or "blocked before evidence spec" in stderr.lower()


def test_block_is_permission_mode_independent():
    """The same non-whitelisted command blocks identically in default mode and
    bypass mode -- the gate ignores permission_mode entirely."""
    rc_default, _ = run_pre_tool_bash("echo hi", permission_mode="default")
    rc_bypass, _ = run_pre_tool_bash("echo hi", permission_mode="bypassPermissions")
    assert rc_default == 2 and rc_bypass == 2, (rc_default, rc_bypass)


def test_whitelisted_bash_passes_under_bypass_permissions():
    """Research-whitelisted commands stay available before a spec exists, even
    under bypass mode -- the gate is a lockdown, not a blanket block."""
    rc, stderr = run_pre_tool_bash("rg --files", permission_mode="bypassPermissions")
    assert rc == 0, f"expected pass (rc 0), got {rc}; stderr={stderr!r}"


def test_light_grade_waives_bash_gate():
    """A LIGHT/quick task waives the bash gate -- routine shell is not
    over-gated on trivial work."""
    rc, _ = run_pre_tool_bash("echo hi", grade="LIGHT", permission_mode="bypassPermissions")
    assert rc == 0


# ---------------------------------------------------------------------------
# Runner (mirrors test_spec_gate.py so the file runs standalone too)
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  OK  {fn.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
