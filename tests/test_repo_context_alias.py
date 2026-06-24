"""§7 regression: the spec citation field was renamed `must_read` -> `repo_context`.

A session that authored a spec under the OLD field name (e.g. its gate was
upgraded mid-flight, or an on-disk spec predates the rename) must still validate,
or the upgrade strands it: the evidence gate blocks every edit and the completion
gate blocks Stop, with no in-session way to rewrite the protected spec. These
tests lock the back-compat alias: `must_read` is accepted as a fallback for
`repo_context` everywhere the field is read, while new specs always write
`repo_context`.
"""

from __future__ import annotations

import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

from citations import empty_activity, verify_citations  # noqa: E402
from spec import repo_context_of, validate_spec  # noqa: E402


def _evidence_spec(field: str) -> dict:
    return {
        "restated_goal": "do x",
        "acceptance_criteria": [{"check": "true", "evidence": "ok"}],
        field: [{"cite": "a.py:1", "why": "relevant because"}],
        "prior_art": [{"cite": "https://example.com", "why": "backs the approach"}],
    }


def test_legacy_must_read_spec_validates_at_standard():
    """A spec carrying citations under the old `must_read` key passes the evidence
    gate at STANDARD -- the upgrade does not strand it."""
    ok, reasons = validate_spec(_evidence_spec("must_read"), "STANDARD", require_evidence=True)
    assert ok, reasons


def test_repo_context_spec_still_validates():
    """The canonical field still works (no regression)."""
    ok, reasons = validate_spec(_evidence_spec("repo_context"), "STANDARD", require_evidence=True)
    assert ok, reasons


def test_repo_context_of_prefers_repo_context_when_both_present():
    spec = {
        "repo_context": [{"cite": "new.py:1", "why": "w"}],
        "must_read": [{"cite": "old.py:1", "why": "w"}],
    }
    got = repo_context_of(spec)
    assert got and got[0]["cite"] == "new.py:1"


def test_repo_context_of_falls_back_to_must_read():
    got = repo_context_of({"must_read": [{"cite": "old.py:1", "why": "w"}]})
    assert got and got[0]["cite"] == "old.py:1"


def test_repo_context_of_empty_when_neither():
    assert repo_context_of({"restated_goal": "x"}) == []


def test_citation_check_reads_must_read_field():
    """The cite-vs-activity cross-check must inspect a legacy `must_read` cite, so a
    fabricated legacy citation is still caught (an unread file is flagged)."""
    reasons = verify_citations(_evidence_spec("must_read"), empty_activity(), cwd=".", require_commands=False)
    assert any("a.py:1" in r for r in reasons), reasons
