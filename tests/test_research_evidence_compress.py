#!/usr/bin/env python3
"""Research-output compression for the Stop validation judge.

Replaces the legacy ledger.redact path (newline-flatten + head-truncate) for
explore trace.sh/websearch.sh stdout with an order-preserving salience filter:
the opening summary, every URL / file:line code ref / section header, and the
closing conclusion survive within budget; bulk prose is dropped. These tests
pin the properties the legacy path violated -- newline preservation and tail
(conclusion / mid-document code-ref) retention -- plus secret redaction.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

from parse_tool_result import (  # noqa: E402
    RESEARCH_BASH_EVIDENCE_CHARS,
    compress_research_output,
    research_bash_evidence,
)


def _big_explore_output() -> str:
    head = "Tracing the widget pipeline across modules.\n\n## Overview\nopening summary line.\n"
    filler = "\n".join(
        f"Line {i}: ordinary prose describing the flow in detail, padding the body well past budget."
        for i in range(80)
    )
    mid = (
        "\n\n```120:140:scripts/gate/zonly.py\n"
        "def z():\n    return 1\n```\n"
        "See https://docs.example.com/deep/reference for the protocol.\n"
    )
    tail = (
        "\n\n## Recommendation\n"
        "FINAL_TOKEN_Z: adopt append-only specs. Source: https://example.org/final\n\n"
        "## Key files\n| File | Role |\n|---|---|\n| scripts/gate/spec.py | judge |\n"
    )
    return head + filler + mid + filler + tail


# --------------------------------------------------------------------------- #
# compress_research_output: unit properties
# --------------------------------------------------------------------------- #
def test_under_budget_passthrough_preserves_newlines():
    txt = "Findings\n- one\n- two\n## Recommendation\nship it"
    out = compress_research_output(txt, RESEARCH_BASH_EVIDENCE_CHARS)
    assert "\n" in out
    assert "Recommendation" in out
    assert "ship it" in out


def test_redacts_secret_on_a_kept_line():
    # Construct the fake credential at runtime so no literal secret sits in the
    # source tree (keeps the gitleaks pre-commit hook green); it still matches
    # ledger.SECRET_PATTERNS once assembled.
    fake = "sk-" + ("z" * 20)
    txt = f"Findings\n- leaked: api_key = '{fake}'\n- done"
    out = compress_research_output(txt, RESEARCH_BASH_EVIDENCE_CHARS)
    assert fake not in out
    assert "[REDACTED]" in out
    assert "\n" in out  # redaction does not flatten structure


def test_over_budget_keeps_newlines_conclusion_and_midref():
    big = _big_explore_output()
    assert len(big) > 2 * RESEARCH_BASH_EVIDENCE_CHARS
    out = compress_research_output(big, RESEARCH_BASH_EVIDENCE_CHARS)
    assert len(out) <= RESEARCH_BASH_EVIDENCE_CHARS
    assert "\n" in out  # structure preserved (legacy redact flattened this)
    # conclusion lives at the tail -- legacy head-truncation dropped it
    assert "FINAL_TOKEN_Z" in out
    assert "https://example.org/final" in out
    # a code ref and URL that sit PAST the 4000-char head window
    assert "120:140:scripts/gate/zonly.py" in out
    assert "docs.example.com/deep/reference" in out


def test_over_budget_drops_bulk_prose():
    big = _big_explore_output()
    out = compress_research_output(big, RESEARCH_BASH_EVIDENCE_CHARS)
    # the repetitive filler is not all retained; a drop marker is present
    assert out.count("Line ") < 80
    assert "..." in out  # an elision marker survives


# --------------------------------------------------------------------------- #
# research_bash_evidence: end-to-end through the explore-script gate
# --------------------------------------------------------------------------- #
def test_research_bash_evidence_retains_tail_and_structure():
    big = _big_explore_output()
    inp = {
        "tool_name": "Bash",
        "tool_input": {"command": 'bash ~/.agents/skills/explore/scripts/websearch.sh "widget"'},
        "tool_response": {"stdout": big},
    }
    ev = research_bash_evidence(inp)
    assert ev is not None
    assert ev.startswith("websearch.sh: ")
    assert "\n" in ev
    assert "FINAL_TOKEN_Z" in ev
    assert "120:140:scripts/gate/zonly.py" in ev
    assert len(ev) <= RESEARCH_BASH_EVIDENCE_CHARS + len("websearch.sh: ") + 4


def test_research_bash_evidence_short_input_unchanged_substrings():
    inp = {
        "tool_name": "Bash",
        "tool_input": {"command": "./websearch.sh goal"},
        "tool_response": {"stdout": "Verified facts\n- x https://example.com/h\nRecommendation\nuse hooks"},
    }
    ev = research_bash_evidence(inp)
    assert ev is not None
    assert ev.startswith("websearch.sh: ")
    assert "https://example.com/h" in ev
    assert "Recommendation" in ev
