#!/usr/bin/env python3
"""Action-only PreToolUse block formatting (scaffold / footer / allowlist context)."""

from __future__ import annotations

import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import pretool_block as pb  # noqa: E402


def test_bash_research_leads_with_why_only_when_scaffold_notified():
    ctx = pb.BlockContext(scaffold_notified=True, unlock_footer_sent=True, allowlist_sent=True)
    msg = pb.format_bash_research_block("npm is not in the Bash research whitelist", ctx=ctx)
    assert msg == "npm is not in the Bash research whitelist."
    assert "Bash blocked" not in msg
    assert "Unlock:" not in msg
    assert "Allowed now:" not in msg


def test_bash_research_includes_unlock_without_scaffold():
    ctx = pb.BlockContext()
    msg = pb.format_bash_research_block("npm is not in the Bash research whitelist", ctx=ctx)
    assert "npm is not in the Bash research whitelist." in msg
    assert "Unlock:" in msg
    assert "Allowed now:" in msg


def test_bash_research_skips_unlock_when_footer_sent():
    ctx = pb.BlockContext(unlock_footer_sent=True)
    msg = pb.format_bash_research_block("grep is not in the Bash research whitelist", ctx=ctx)
    assert "Unlock:" not in msg
    assert "Allowed now:" in msg


def test_delegation_empty_when_scaffold_and_allowlist_sent():
    ctx = pb.BlockContext(scaffold_notified=True, unlock_footer_sent=True, allowlist_sent=True)
    assert pb.format_delegation_block("Task", ctx=ctx) == ""


def test_spec_missing_silent_when_scaffold_notified():
    ctx = pb.BlockContext(scaffold_notified=True)
    assert pb.format_spec_missing_block("STANDARD", "s1", "contract text", ctx=ctx) == ""


def test_spec_missing_unlock_when_no_scaffold():
    ctx = pb.BlockContext()
    msg = pb.format_spec_missing_block("STANDARD", "s1", "contract text", ctx=ctx)
    assert "Evidence spec required" in msg
    assert "Unlock:" in msg


def test_is_redundant_with_notify():
    assert pb.is_redundant_with_notify(pb._UNLOCK_LINE, "Read foo.py first.")
    assert pb.is_redundant_with_notify("Read foo.py first.", "Read foo.py first.")
    assert not pb.is_redundant_with_notify("npm is not in the Bash research whitelist.", "Read foo.py first.")


def test_is_boilerplate_only():
    assert pb.is_boilerplate_only(pb._UNLOCK_LINE)
    assert not pb.is_boilerplate_only("npm is not in the Bash research whitelist.")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
