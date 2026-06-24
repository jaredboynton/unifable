#!/usr/bin/env python3
"""MCP results as first-class evidence + auto-adjudication of non-runnable checks.

Covers:
  - MCP tool call detection (Claude `mcp__server__tool`, Codex `server.tool`) and
    ledger capture as structured evidence.
  - Prose / natural-language task checks classified non-runnable (never shell-
    executed; routed to evidence_only judging instead of the exit-127 loop).
  - Evidence corpus surfacing into the Stop validation payload.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import spec as spec_mod  # noqa: E402
from ledger import add_unique, default_ledger  # noqa: E402
from parse_tool_result import is_mcp_tool, mcp_evidence  # noqa: E402
from spec import (  # noqa: E402
    _build_validate_all_user,
    _evidence_payload,
    auto_validate_spec,
    is_runnable_check,
    load_spec,
    save_spec,
    spec_template,
)

# --------------------------------------------------------------------------- #
# is_mcp_tool: both host shapes, no false positives on core tools
# --------------------------------------------------------------------------- #


def test_is_mcp_tool_claude_shape():
    assert is_mcp_tool("mcp__example__slack_search")
    assert is_mcp_tool("mcp__memory__create_entities")


def test_is_mcp_tool_codex_dotted_shape():
    assert is_mcp_tool("example-mcp.slack_search")
    assert is_mcp_tool("octocode.githubSearchPullRequests")


def test_is_mcp_tool_core_tools_are_not_mcp():
    for name in ("Read", "Bash", "apply_patch", "Edit", "Write", "WebFetch", "Grep", "Glob", "NotebookEdit", ""):
        assert not is_mcp_tool(name), name


# --------------------------------------------------------------------------- #
# mcp_evidence: compact "<tool>: <result>" capture
# --------------------------------------------------------------------------- #


def test_mcp_evidence_captures_codex_result():
    inp = {
        "tool_name": "example-mcp.slack_search",
        "tool_response": {"messages": {"matches": [{"content": "rollout completed for Example Corp"}]}},
    }
    ev = mcp_evidence(inp)
    assert ev is not None
    assert ev.startswith("example-mcp.slack_search: ")
    assert "Example Corp" in ev


def test_mcp_evidence_none_for_non_mcp_tool():
    assert mcp_evidence({"tool_name": "Bash", "tool_response": "ls output"}) is None
    assert mcp_evidence({"tool_name": "Read", "tool_response": "file body"}) is None


def test_mcp_evidence_capture_into_ledger():
    """The 3-line gate_post_tool wiring: mcp_evidence -> add_unique('tool_evidence')."""
    led = default_ledger()
    assert led.get("tool_evidence") == []
    inp = {"tool_name": "octocode.githubSearchPullRequests", "tool_response": {"data": "PR 42 open draft"}}
    ev = mcp_evidence(inp)
    assert ev
    add_unique(led, "tool_evidence", [ev])
    assert led["tool_evidence"] and "PR 42" in led["tool_evidence"][0]


# --------------------------------------------------------------------------- #
# is_runnable_check: prose vs real command
# --------------------------------------------------------------------------- #


def test_prose_checks_are_not_runnable():
    for prose in (
        "Slack search returned a relevant direct message",
        "Issue tracker ticket documents the export format limitation",
        "Final response cites the verified tool results and separates facts from unknowns",
        "Pull request metadata shows open draft state",
        "",
    ):
        assert not is_runnable_check(prose), prose


def test_real_commands_are_runnable():
    for cmd in (
        "python3 -m pytest tests/test_spec_gate.py -q",
        "test -f docs/x.md && grep -q Foo docs/x.md",
        "git ls-files --error-unmatch docs/x.md",
        "rg -q pattern src/",
        "FOO=bar python3 script.py",
        "./scripts/check.sh",
    ):
        assert is_runnable_check(cmd), cmd


# --------------------------------------------------------------------------- #
# _evidence_payload + _build_validate_all_user: evidence reaches the judge
# --------------------------------------------------------------------------- #


def test_evidence_payload_bounded_and_empty_is_none():
    assert _evidence_payload(None) is None
    assert _evidence_payload({}) is None
    ev = _evidence_payload(
        {
            "read_paths": ["/a.py"],
            "tool_evidence": ["mcp__x__y: hello"],
        }
    )
    assert ev["read_paths"] == ["/a.py"]
    assert ev["tool_results"] == ["mcp__x__y: hello"]


def test_build_validate_payload_marks_evidence_only_and_includes_evidence():
    s = spec_template()
    s["restated_goal"] = "research goal"
    items = [
        {
            "task": {"id": "T1", "title": "collect slack", "check": "slack returned the DM", "status": "pending"},
            "kind": "validate",
            "evidence_only": True,
            "exit_code": None,
            "output": "",
        },
        {
            "task": {"id": "T2", "title": "run tests", "check": "pytest -q", "status": "pending"},
            "kind": "validate",
            "exit_code": 0,
            "output": "1 passed",
        },
    ]
    evidence = {"read_paths": ["/a.py"], "tool_evidence": ["example-mcp.slack_search: message about the release"]}
    payload = json.loads(_build_validate_all_user(s, items, None, evidence))

    t1, t2 = payload["tasks_to_adjudicate"]
    assert t1["evidence_only"] is True and t1["exit_code"] is None
    assert "evidence_only" not in t2 and t2["exit_code"] == 0
    assert payload["evidence"]["tool_results"] == ["example-mcp.slack_search: message about the release"]


# --------------------------------------------------------------------------- #
# auto_validate_spec: the loop fix end-to-end
# --------------------------------------------------------------------------- #


def _spec_with_check(check: str, status: str = "pending") -> dict:
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [{"id": "T1", "title": "research", "check": check, "status": status}]
    return s


def test_prose_check_routed_evidence_only_and_never_shell_run(tmp_path, monkeypatch):
    """A prose check is classified evidence_only and is NEVER passed to run_check."""
    save_spec(str(tmp_path), "K", _spec_with_check("Slack search returned a relevant direct message"))

    def boom_run_check(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("run_check was invoked on a prose check (doom-loop regression)")

    captured = {}

    def fake_judge_tasks(sp, items, *, transcript="", evidence=None, **kw):
        captured["items"] = items
        captured["evidence"] = evidence
        return [(1, "evidence supports it", [], "") for _ in items]

    monkeypatch.setattr(spec_mod, "run_check", boom_run_check)
    monkeypatch.setattr(spec_mod, "judge_tasks", fake_judge_tasks)

    s2, _ = auto_validate_spec(
        load_spec(str(tmp_path), "K"),
        str(tmp_path),
        evidence={"tool_evidence": ["example-mcp.slack_search: direct message about the release"]},
    )

    assert captured["items"][0].get("evidence_only") is True
    assert captured["evidence"]["tool_evidence"] == ["example-mcp.slack_search: direct message about the release"]
    assert s2["tasks"][0]["status"] == "validated"


def test_command_not_found_backstops_to_evidence_only(tmp_path, monkeypatch):
    """A check we classify runnable but the shell can't resolve (exit 127, command
    not found) is also routed to evidence_only rather than recorded as failed."""
    save_spec(str(tmp_path), "K", _spec_with_check("pytest tests/does_not_exist.py"))

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (127, "/bin/sh: pytest: command not found"))

    captured = {}

    def fake_judge_tasks(sp, items, *, transcript="", evidence=None, **kw):
        captured["items"] = items
        return [(1, "ok", [], "") for _ in items]

    monkeypatch.setattr(spec_mod, "judge_tasks", fake_judge_tasks)
    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert captured["items"][0].get("evidence_only") is True
