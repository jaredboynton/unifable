#!/usr/bin/env python3
"""Tests for scripts/gate/tool_scope.py — the deterministic per-step tool scope.

The director judge persists a tool scope into breaker state; this module decides,
with NO judge call, whether an imminent tool is in scope. It must:
  - fail open (empty/malformed scope allows everything),
  - honor an explicit deny-list and an explicit allow-list,
  - never brick the grounding floor (Read/Grep/Glob/WebSearch/WebFetch) so the
    agent can always ground a claim and escape a bad scope,
  - surface the director's directive as the block reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parent.parent / "scripts" / "gate"
if str(GATE_DIR) not in sys.path:
    sys.path.insert(0, str(GATE_DIR))

import tool_scope  # noqa: E402


def test_empty_scope_allows_everything() -> None:
    for scope in (None, {}, {"allow": [], "deny": []}):
        ok, reason = tool_scope.in_scope("Edit", scope)
        assert ok is True
        assert reason == ""


def test_malformed_scope_fails_open() -> None:
    for scope in ("not a dict", 42, {"allow": "Edit"}, {"deny": 7}):
        ok, _ = tool_scope.in_scope("Edit", scope)
        assert ok is True


def test_deny_list_blocks_named_tool() -> None:
    scope = {"deny": ["Edit", "Bash"], "directive": "Read the source first."}
    blocked, reason = tool_scope.in_scope("Edit", scope)
    assert blocked is False
    assert "Read the source first." in reason
    # A tool not on the deny-list passes.
    ok, _ = tool_scope.in_scope("Write", scope)
    assert ok is True


def test_allow_list_is_exclusive() -> None:
    scope = {"allow": ["Read", "Bash"]}
    ok, _ = tool_scope.in_scope("Bash", scope)
    assert ok is True
    blocked, _ = tool_scope.in_scope("Edit", scope)
    assert blocked is False


def test_grounding_floor_never_blocked() -> None:
    # Even if the scope explicitly denies reads or allow-lists only Edit, the
    # grounding tools stay reachable so the agent can never be bricked.
    for scope in ({"deny": ["Read", "Grep"]}, {"allow": ["Edit"]}):
        for tool in ("Read", "Grep", "Glob", "WebSearch", "WebFetch"):
            ok, _ = tool_scope.in_scope(tool, scope)
            assert ok is True, f"{tool} must stay reachable under {scope}"


def test_directive_default_reason_when_unset() -> None:
    scope = {"deny": ["Edit"]}
    blocked, reason = tool_scope.in_scope("Edit", scope)
    assert blocked is False
    assert reason.strip() != ""  # a non-empty default block reason


def test_scope_and_directive_readers_from_state() -> None:
    state = {"breaker_tool_scope": {"deny": ["Edit"]}, "breaker_directive": "next: read foo.py"}
    assert tool_scope.scope_from_state(state) == {"deny": ["Edit"]}
    assert tool_scope.current_directive(state) == "next: read foo.py"
    # Absent keys -> safe defaults.
    assert tool_scope.scope_from_state({}) == {}
    assert tool_scope.current_directive({}) == ""


def test_pretool_enforces_persisted_scope(tmp_path, monkeypatch) -> None:
    """The PreToolUse helper blocks an out-of-scope tool using the persisted scope,
    allows in-scope tools, and never blocks the grounding floor -- no judge call."""
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    repo = Path(__file__).resolve().parent.parent
    for p in (str(repo / "hooks"), str(repo / "scripts" / "gate")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib

    import breaker_state

    importlib.reload(breaker_state)
    import pre_tool_use

    importlib.reload(pre_tool_use)

    input_data = {"session_id": "scope-sess", "cwd": str(tmp_path)}
    state = breaker_state.default_breaker()
    state["breaker_tool_scope"] = {"deny": ["Edit"], "directive": "Read foo.py before editing."}
    state["breaker_directive"] = "Read foo.py before editing."
    breaker_state.save_breaker(input_data, state)

    # Edit is denied -> blocking exit code 2.
    assert pre_tool_use._enforce_tool_scope(input_data, "Edit", "") == 2
    # Write is not on the deny-list -> allowed (None, fall through).
    assert pre_tool_use._enforce_tool_scope(input_data, "Write", "") is None
    # Grounding floor stays reachable even if scope is restrictive.
    assert pre_tool_use._enforce_tool_scope(input_data, "Read", "") is None


def test_pretool_no_scope_is_noop(tmp_path, monkeypatch) -> None:
    """With no persisted scope, the helper returns None (fail-open, no block)."""
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    repo = Path(__file__).resolve().parent.parent
    for p in (str(repo / "hooks"), str(repo / "scripts" / "gate")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib

    import pre_tool_use

    importlib.reload(pre_tool_use)
    input_data = {"session_id": "noscope-sess", "cwd": str(tmp_path)}
    assert pre_tool_use._enforce_tool_scope(input_data, "Edit", "") is None


def _reload_pre_tool_use(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    repo = Path(__file__).resolve().parent.parent
    for p in (str(repo / "hooks"), str(repo / "scripts" / "gate")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib

    import breaker_state

    importlib.reload(breaker_state)
    import pre_tool_use

    importlib.reload(pre_tool_use)
    return breaker_state, pre_tool_use


def test_pretool_scope_allows_research_bash(tmp_path, monkeypatch) -> None:
    """A content-revealing search (grep/rg) and the spec CLI pass the director scope
    even when it denies Bash -- the director steers mutations, not the agent's
    evidence-gathering. Non-research Bash stays scope-blocked."""
    breaker_state, pre_tool_use = _reload_pre_tool_use(tmp_path, monkeypatch)

    def _inp(cmd):
        return {
            "session_id": "rb",
            "cwd": str(tmp_path),
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
        }

    state = breaker_state.default_breaker()
    state["breaker_tool_scope"] = {"deny": ["Bash"], "directive": "Read foo.py first."}
    state["breaker_directive"] = "Read foo.py first."
    breaker_state.save_breaker(_inp("grep"), state)

    # Research Bash passes even though the scope denies Bash.
    assert pre_tool_use._enforce_tool_scope(_inp("grep -n foo bar.py"), "Bash", "") is None
    assert pre_tool_use._enforce_tool_scope(_inp("rg foo"), "Bash", "") is None
    assert pre_tool_use._enforce_tool_scope(_inp("unifable restate 'x'"), "Bash", "") is None
    # Non-research Bash is still blocked by the scope (research phase, no valid spec).
    assert pre_tool_use._enforce_tool_scope(_inp("python3 evil.py"), "Bash", "") == 2


def test_pretool_scope_block_emits_directive_once(tmp_path, monkeypatch, capsys) -> None:
    """A scope block surfaces the directive ONCE -- not duplicated as both
    'unifable pre-edit gate: <directive>' and 'unifable director: <directive>'."""
    breaker_state, pre_tool_use = _reload_pre_tool_use(tmp_path, monkeypatch)

    directive = "Read foo.py before editing."
    input_data = {"session_id": "dedupe", "cwd": str(tmp_path)}
    state = breaker_state.default_breaker()
    state["breaker_tool_scope"] = {"deny": ["Edit"], "directive": directive}
    state["breaker_directive"] = directive
    breaker_state.save_breaker(input_data, state)

    rc = pre_tool_use._enforce_tool_scope(input_data, "Edit", f"unifable director: {directive}")
    assert rc == 2
    err = capsys.readouterr().err
    assert err.count(directive) == 1, err
    assert "unifable director:" not in err
    assert "unifable pre-edit gate:" in err


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
