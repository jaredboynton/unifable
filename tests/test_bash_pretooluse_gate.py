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
    no prior ledger state leaks in. Citation truth-check is disabled so the test
    is offline and deterministic; this exercises the research-whitelist branch
    only (no transcript -> groundedness arm judge does not fire).
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


def test_both_manifests_route_all_post_tool_results_to_gate_post_tool():
    """gate_post_tool must see every successful tool result, including MCP tool
    output, or the breaker/judge can miss evidence that already appeared in the
    transcript."""
    for host, path in MANIFESTS.items():
        matcher = _post_tool_matcher(path, "gate_post_tool.py")
        assert matcher is not None, f"{host}: gate_post_tool is not wired on PostToolUse"
        for tool in ("Read", "WebFetch", "Bash", "mcp__octocode__githubGetFileContent"):
            assert re.match(matcher, tool), (
                f"{host}: gate_post_tool matcher {matcher!r} drops {tool!r} -- "
                "tool results from that tool will not log"
            )


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


def test_readonly_git_passes_under_bypass_permissions():
    """Read-only git (status/diff/log) is available before a valid spec exists."""
    rc, stderr = run_pre_tool_bash("git status --short", permission_mode="bypassPermissions")
    assert rc == 0, f"expected pass (rc 0), got {rc}; stderr={stderr!r}"


def test_light_grade_waives_bash_gate():
    """A LIGHT/quick task waives the bash gate -- routine shell is not
    over-gated on trivial work."""
    rc, _ = run_pre_tool_bash("echo hi", grade="LIGHT", permission_mode="bypassPermissions")
    assert rc == 0


def _seed_armed_breaker(data_root: str, session_id: str, cwd: str) -> None:
    """Write ledger + armed breaker state so pre_tool_use sees an active arm."""
    import hashlib
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(REPO / "scripts" / "gate"))
    from breaker_state import default_breaker  # noqa: E402
    from groundedness import arm  # noqa: E402

    key = hashlib.sha256(f"{session_id}|{cwd}".encode()).hexdigest()[:24]
    root = Path(data_root)
    ledger_path = root / "ledgers" / f"{key}.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps({"active_task": "P", "read_paths": [], "fetched_urls": [], "ran_commands": []}),
        encoding="utf-8",
    )
    breaker = default_breaker()
    arm(breaker, f"{session_id}|P", 0.0, "read scorer source and cite evidence", "unproven scoring split")
    breaker_path = root / "breaker" / f"{key}.json"
    breaker_path.parent.mkdir(parents=True, exist_ok=True)
    breaker_path.write_text(json.dumps(breaker), encoding="utf-8")


def _run_pre_tool_bash_breaker_on(
    command: str,
    *,
    session_id: str = "breaker-bash-test",
) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as tmp:
        _seed_armed_breaker(tmp, session_id, tmp)
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "session_id": session_id,
            "cwd": tmp,
            "permission_mode": "bypassPermissions",
        }
        env = dict(os.environ)
        env["UNIFABLE_GRADE"] = "STANDARD"
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


def test_whitelisted_bash_passes_while_breaker_armed():
    """Research Bash (rg/ls/glob/trace.sh/spec CLI) must stay available when the
    groundedness breaker is armed -- matches unifable-block.md guidance."""
    rc, stderr = _run_pre_tool_bash_breaker_on("rg --files")
    assert rc == 0, f"expected pass (rc 0), got {rc}; stderr={stderr!r}"


def test_mutating_bash_blocked_while_breaker_armed():
    """Non-whitelisted Bash stays blocked when the breaker is armed."""
    rc, stderr = _run_pre_tool_bash_breaker_on("node scripts/score.mjs")
    assert rc == 2, f"expected block (rc 2), got {rc}; stderr={stderr!r}"
    assert "groundedness" in stderr.lower() or "pre-edit gate" in stderr.lower()


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
