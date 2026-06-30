#!/usr/bin/env python3
"""PreToolUse coverage for MCP tools.

Claude and Codex expose MCP tools as hook-visible tool names. Read-like MCP
tools must stay on the grounding floor, while mutation-like MCP tools need the
same spec/protected-path gates as Edit/Write/Bash.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks"
GATE = REPO / "scripts" / "gate"
PY = sys.executable


def _run_pre_tool(payload: dict, *, data_root: str, grade: str = "STANDARD", host: str = "codex") -> tuple[int, str, str]:
    env = dict(os.environ)
    env["UNIFABLE_GRADE"] = grade
    env["UNIFABLE_VERIFY_CITATIONS"] = "0"
    env["UNIFABLE_DATA"] = data_root
    env["UNIFABLE_HOST"] = host
    proc = subprocess.run(
        [PY, str(HOOKS / "pre_tool_use.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_read_like_mcp_tool_stays_allowed_without_spec():
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "tool_name": "mcp__filesystem__read_file",
            "tool_input": {"path": "src/app.py"},
            "session_id": "mcp-read",
            "cwd": tmp,
        }
        rc, stdout, stderr = _run_pre_tool(payload, data_root=tmp)
        assert rc == 0, (stdout, stderr)


def test_read_like_mcp_name_with_write_payload_is_mutation_gated():
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "tool_name": "mcp__filesystem__read_file",
            "tool_input": {"path": "src/app.py", "content": "replacement"},
            "session_id": "mcp-read-name-write-payload",
            "cwd": tmp,
            "turn_id": "turn-mcp-read-name-write-payload",
        }
        rc, stdout, stderr = _run_pre_tool(payload, data_root=tmp)
        assert rc == 2, (stdout, stderr)
        assert "Evidence spec required" in stderr


def test_read_like_mcp_name_with_graphql_mutation_payload_is_gated():
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "tool_name": "mcp__github__query",
            "tool_input": {"query": "mutation CreateIssue { createIssue(input: {}) { id } }"},
            "session_id": "mcp-read-name-graphql-mutation",
            "cwd": tmp,
            "turn_id": "turn-mcp-read-name-graphql-mutation",
        }
        rc, stdout, stderr = _run_pre_tool(payload, data_root=tmp)
        assert rc == 2, (stdout, stderr)
        assert "Evidence spec required" in stderr


def test_read_like_mcp_name_with_select_sql_payload_stays_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "tool_name": "mcp__db__query",
            "tool_input": {"sql": "SELECT * FROM users"},
            "session_id": "mcp-read-name-select",
            "cwd": tmp,
            "turn_id": "turn-mcp-read-name-select",
        }
        rc, stdout, stderr = _run_pre_tool(payload, data_root=tmp)
        assert rc == 0, (stdout, stderr)


def test_read_like_mcp_tool_is_not_a_director_scoped_mutation():
    for path in (HOOKS, GATE):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import importlib

    import pre_tool_use

    importlib.reload(pre_tool_use)

    assert pre_tool_use._is_gated_tool("mcp__exa__web_search_exa") is False
    assert pre_tool_use._is_gated_tool("mcp__filesystem__read_file") is False
    assert pre_tool_use._is_gated_tool("mcp__github__create_issue") is True


def test_mcp_mutation_blocks_without_valid_spec():
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "tool_name": "mcp__github__create_issue",
            "tool_input": {"title": "bug", "body": "details"},
            "session_id": "mcp-write",
            "cwd": tmp,
            "turn_id": "turn-mcp-write",
        }
        rc, stdout, stderr = _run_pre_tool(payload, data_root=tmp)
        assert rc == 2, (stdout, stderr)
        assert "Evidence spec required" in stderr


def test_claude_full_pretool_path_emits_structured_deny_for_mcp_mutation():
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "tool_name": "mcp__github__create_issue",
            "tool_input": {"title": "bug", "body": "details"},
            "session_id": "mcp-claude-deny",
            "cwd": tmp,
            "hook_event_name": "PreToolUse",
        }
        rc, stdout, stderr = _run_pre_tool(payload, data_root=tmp, host="claude")
        parsed = json.loads(stdout)

        assert rc == 0, (stdout, stderr)
        assert stderr == ""
        hso = parsed["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "deny"
        assert hso["permissionDecisionReason"]


def test_claude_structured_deny_full_path_is_single_json_for_core_block_kinds():
    cases = [
        (
            "spec",
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "src/app.py", "old_string": "x", "new_string": "y"},
            },
        ),
        (
            "bash",
            {
                "tool_name": "Bash",
                "tool_input": {"command": "npm test"},
            },
        ),
        (
            "delegation",
            {
                "tool_name": "Task",
                "tool_input": {"description": "worker", "prompt": "fix it"},
            },
        ),
        (
            "protected",
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": ".unifable/state.json", "old_string": "x", "new_string": "y"},
            },
        ),
    ]
    for name, partial in cases:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                **partial,
                "session_id": f"claude-{name}-deny",
                "cwd": tmp,
                "hook_event_name": "PreToolUse",
                "turn_id": f"turn-claude-{name}-deny",
            }
            rc, stdout, stderr = _run_pre_tool(payload, data_root=tmp, host="claude")
            assert rc == 0, (name, stdout, stderr)
            assert stderr == "", (name, stderr)
            assert stdout.endswith("\n") and stdout.count("\n") == 1, (name, stdout)
            parsed = json.loads(stdout)
            hso = parsed["hookSpecificOutput"]
            assert hso["hookEventName"] == "PreToolUse", name
            assert hso["permissionDecision"] == "deny", name
            assert hso["permissionDecisionReason"], name


def test_codex_apply_patch_blocks_with_exit2_and_stderr():
    patch = (
        "*** Begin Patch\n"
        "*** Update File: src/app.py\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        payload = {
            "tool_name": "apply_patch",
            "tool_input": {"patch": patch},
            "session_id": "codex-applypatch-block",
            "cwd": tmp,
            "turn_id": "turn-codex-applypatch-block",
        }
        rc, stdout, stderr = _run_pre_tool(payload, data_root=tmp, host="codex")
        assert rc == 2, (stdout, stderr)
        assert stdout == ""
        assert "Evidence spec required" in stderr


def test_mcp_mutation_protected_path_blocks_even_for_light_grade():
    with tempfile.TemporaryDirectory() as tmp:
        protected = str(Path(tmp) / ".unifable" / "state.json")
        payload = {
            "tool_name": "mcp__filesystem__write_file",
            "tool_input": {"path": protected, "content": "{}"},
            "session_id": "mcp-protected",
            "cwd": tmp,
            "turn_id": "turn-mcp-protected",
        }
        rc, stdout, stderr = _run_pre_tool(payload, data_root=tmp, grade="LIGHT")
        assert rc == 2, (stdout, stderr)
        assert "Protected unifable state" in stderr
