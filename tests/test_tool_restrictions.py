#!/usr/bin/env python3
"""Canonical hook-visible tool restriction copy."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATE = ROOT / "scripts" / "gate"
HOOKS = ROOT / "hooks"
for path in (str(GATE), str(HOOKS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import tool_restrictions as tr  # noqa: E402


def _pre_tool_matcher(manifest_path: Path) -> str:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    for group in data["hooks"]["PreToolUse"]:
        for hook in group.get("hooks", []):
            if "pre_tool_use.py" in str(hook.get("command", "")):
                return str(group.get("matcher", ""))
    raise AssertionError(f"{manifest_path}: no pre_tool_use.py matcher")


def test_groundedness_footer_lists_exact_hook_visible_tools() -> None:
    footer = tr.groundedness_restriction_footer()

    assert f"Available inspection tools: {tr.inspection_tools_csv()}." in footer
    assert f"Shell/REPL tools ({tr.shell_tools_csv()}):" in footer
    assert f"Blocked until grounded: {tr.groundedness_blocked_tools_csv()}." in footer


def test_legacy_groundedness_restriction_copy_is_stripped() -> None:
    old = (
        "The claim is unproven. Restrict tools to \n"
        "read-only ones (Read, WebSearch, WebFetch, Grep, Glob) and whitelisted research Bash "
        "until this is grounded. Read the relevant source."
    )
    msg = tr.groundedness_block_message(old)

    assert msg.strip()

    nested = (
        "Your tools are restricted to read-only ones (Read, WebSearch, WebFetch, Grep, Glob) "
        "and whitelisted research Bash (cd, ls, echo (sink pipes only), read-only git) "
        "until you ground the claim. Inspect the fixture output."
    )
    msg = tr.groundedness_block_message(nested)
    assert msg.strip()


def test_pretool_manifest_matchers_sync_with_canonical_gated_tools() -> None:
    expected = tr.pretool_matcher_regex()
    for rel in ("hooks/hooks.json", ".codex-plugin/hooks.json"):
        matcher = _pre_tool_matcher(ROOT / rel)
        assert matcher == expected
        for tool in tr.PRETOOL_GATED_TOOLS:
            assert re.match(matcher, tool), f"{rel}: matcher dropped {tool!r}"
        assert re.match(matcher, "mcp__github__create_issue"), f"{rel}: matcher dropped MCP tools"
        assert re.match(matcher, "mcp__filesystem__read_file"), f"{rel}: matcher dropped read-like MCP tools"
        for tool in ("Read", "WebSearch", "WebFetch", "webrun", "Grep", "Glob", "TodoWrite"):
            assert not re.match(matcher, tool), f"{rel}: matcher unexpectedly catches {tool!r}"


def test_mcp_tool_classification_is_read_like_or_mutation() -> None:
    assert tr.is_mcp_read_like_tool("mcp__filesystem__read_file")
    assert tr.is_mcp_read_like_tool("mcp__github__search_repositories")
    assert tr.is_mcp_mutation_tool("mcp__github__create_issue")
    assert tr.is_mcp_mutation_tool("mcp__memory__delete_entities")
    assert tr.is_mcp_mutation_tool("mcp__storage__upload_file")
    assert tr.is_mcp_mutation_tool("mcp__x__recompute")
    assert not tr.is_mcp_mutation_tool("WebSearch")
    assert tr.mcp_input_forces_mutation({"query": "UPDATE users SET name = 'x'"})
    assert tr.mcp_input_forces_mutation({"path": "x", "content": "new"})
    assert not tr.mcp_input_forces_mutation({"query": "SELECT * FROM users"})


def test_pretool_breaker_block_appends_hook_owned_footer(tmp_path, monkeypatch, capsys) -> None:
    import breaker_orchestration
    import pre_tool_use

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))

    legacy = (
        "The claim is unproven. Your tools are restricted to read-only ones "
        "(Read, WebSearch, WebFetch, Grep, Glob) and whitelisted research Bash "
        "until you ground the claim."
    )

    def fake_eval(_input_data, _now, _active):
        return True, legacy, "", {"events": []}

    monkeypatch.setattr(breaker_orchestration, "evaluate_pre_tool_locked", fake_eval)
    rc, notify = pre_tool_use._enforce_breaker(
        {"tool_name": "Edit", "session_id": "footer", "cwd": str(tmp_path)}
    )
    err = capsys.readouterr().err

    assert rc == 2
    assert notify == ""
    assert err.strip()
