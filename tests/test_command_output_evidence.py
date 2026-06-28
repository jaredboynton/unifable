#!/usr/bin/env python3
"""Bounded command-output capture into the Stop evidence corpus.

Closes the sibling gap to the MCP-evidence work: ran_commands recorded the
command STRING but not its OUTPUT, so a generic shell probe (curl HTTP body, cat
of a config showing an ETag) left no durable proof for the evidence_only Stop
judge -- the output lived only in the budget-capped transcript tail. This pins
command_output_evidence capture, ledger/db plumbing, the evidence payload, and
the loop fix end-to-end.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import spec_stop_validate as ssv  # noqa: E402
from ledger import add_unique, default_ledger  # noqa: E402
from parse_tool_result import (  # noqa: E402
    COMMAND_OUTPUT_EVIDENCE_CHARS,
    command_output_evidence,
)
from spec import auto_validate_spec, load_spec, save_spec, spec_template  # noqa: E402
from spec_judge import _evidence_payload  # noqa: E402

# --------------------------------------------------------------------------- #
# command_output_evidence: capture shape + boundaries
# --------------------------------------------------------------------------- #


def test_captures_curl_probe_body():
    inp = {
        "tool_name": "Bash",
        "tool_input": {"command": "curl -s https://api.example.com/catalog"},
        "tool_response": {"stdout": 'HTTP/1.1 400 Bad Request\n{"error":"missing etag"}'},
    }
    ev = command_output_evidence(inp)
    assert ev is not None
    assert ev.startswith("curl -s https://api.example.com/catalog: ")
    assert "400 Bad Request" in ev
    assert "missing etag" in ev


def test_captures_cat_config_etag():
    inp = {
        "tool_name": "Bash",
        "tool_input": {"command": "cat catalog.json"},
        "tool_response": {"stdout": '{"etag": "W/\\"abc123\\""}'},
    }
    ev = command_output_evidence(inp)
    assert ev is not None
    assert "abc123" in ev


def test_none_for_non_shell_tool():
    assert command_output_evidence({"tool_name": "Read", "tool_response": "file body"}) is None
    assert command_output_evidence({"tool_name": "WebFetch", "tool_response": "page"}) is None
    assert command_output_evidence({"tool_name": "mcp__x__y", "tool_response": "r"}) is None


def test_none_for_empty_output():
    inp = {"tool_name": "Bash", "tool_input": {"command": "true"}, "tool_response": {"stdout": ""}}
    assert command_output_evidence(inp) is None


def test_deferred_for_explore_script():
    """An explore trace.sh/websearch.sh command is owned by research_bash_evidence,
    so command_output_evidence stays out of the way (no double-recording)."""
    inp = {
        "tool_name": "Bash",
        "tool_input": {"command": "bash ~/.agents/skills/unitrace/scripts/websearch.sh 'x'"},
        "tool_response": {"stdout": "Verified facts\n- something https://example.com/a"},
    }
    assert command_output_evidence(inp) is None


def test_output_is_bounded():
    big = "x" * (COMMAND_OUTPUT_EVIDENCE_CHARS * 3)
    inp = {"tool_name": "Bash", "tool_input": {"command": "cat big.txt"}, "tool_response": {"stdout": big}}
    ev = command_output_evidence(inp)
    assert ev is not None
    assert len(ev) <= COMMAND_OUTPUT_EVIDENCE_CHARS + 8  # small slack for the joiner


def test_secret_redacted():
    inp = {
        "tool_name": "Bash",
        "tool_input": {"command": "cat .env"},
        "tool_response": {"stdout": "api_key=sk-abcdefghijklmnopqrstuvwxyz"},
    }
    ev = command_output_evidence(inp)
    assert ev is not None
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in ev
    assert "[REDACTED]" in ev


# --------------------------------------------------------------------------- #
# ledger + evidence-payload plumbing
# --------------------------------------------------------------------------- #


def test_default_ledger_seeds_command_outputs():
    assert default_ledger().get("command_outputs") == []


def test_capture_into_ledger():
    led = default_ledger()
    inp = {
        "tool_name": "Bash",
        "tool_input": {"command": "curl -s https://x/probe"},
        "tool_response": {"stdout": "HTTP 200 OK"},
    }
    ev = command_output_evidence(inp)
    assert ev
    add_unique(led, "command_outputs", [ev])
    assert led["command_outputs"] and "HTTP 200 OK" in led["command_outputs"][0]


def test_evidence_payload_includes_command_outputs():
    payload = _evidence_payload({"command_outputs": ["curl https://x: HTTP 400 missing etag"]})
    assert payload is not None
    assert payload["command_outputs"] == ["curl https://x: HTTP 400 missing etag"]


def test_evidence_payload_command_outputs_alone_is_a_nonempty_corpus():
    payload = _evidence_payload({"command_outputs": ["cat catalog.json: etag W/abc"]})
    assert payload is not None and any(payload.values())


# --------------------------------------------------------------------------- #
# auto_validate_spec: the loop fix end-to-end
# --------------------------------------------------------------------------- #


def _spec_with_check(check: str) -> dict:
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [{"id": "T1", "title": "probe", "check": check, "status": "pending"}]
    return s


def test_prose_check_validates_off_command_outputs_corpus(tmp_path, monkeypatch):
    """A prose evidence_only task is adjudicated against the command_outputs corpus,
    which must reach the judge."""
    save_spec(str(tmp_path), "K", _spec_with_check("The catalog endpoint returns HTTP 400 for a missing etag"))

    captured = {}

    def fake_judge_tasks(sp, items, *, transcript="", evidence=None, **kw):
        captured["items"] = items
        captured["evidence"] = evidence
        return [(1, "the captured output proves it", [], "") for _ in items]

    monkeypatch.setattr(ssv, "judge_tasks", fake_judge_tasks)

    s2, _ = auto_validate_spec(
        load_spec(str(tmp_path), "K"),
        str(tmp_path),
        evidence={"command_outputs": ["curl https://api/catalog: HTTP 400 Bad Request missing etag"]},
    )

    assert captured["items"][0].get("evidence_only") is True
    assert captured["evidence"]["command_outputs"] == [
        "curl https://api/catalog: HTTP 400 Bad Request missing etag"
    ]
    assert s2["tasks"][0]["status"] == "validated"
