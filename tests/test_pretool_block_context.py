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
    assert msg.strip()
    assert len(msg.splitlines()) == 1


def test_bash_research_includes_unlock_without_scaffold():
    ctx = pb.BlockContext()
    msg = pb.format_bash_research_block("npm is not in the Bash research whitelist", ctx=ctx)
    assert pb._RESTATE_LINE in msg
    assert pb._ADD_TASK_LINE in msg
    assert len(msg.splitlines()) > 3


def test_bash_research_skips_unlock_when_footer_sent():
    ctx = pb.BlockContext(unlock_footer_sent=True)
    msg = pb.format_bash_research_block("grep is not in the Bash research whitelist", ctx=ctx)
    assert msg.strip()


def test_delegation_empty_when_scaffold_and_allowlist_sent():
    ctx = pb.BlockContext(scaffold_notified=True, unlock_footer_sent=True, allowlist_sent=True)
    assert pb.format_delegation_block("Task", ctx=ctx) == ""


def test_spec_missing_silent_when_scaffold_notified():
    ctx = pb.BlockContext(scaffold_notified=True)
    assert pb.format_spec_missing_block("STANDARD", "s1", "contract text", ctx=ctx) == ""


def test_spec_missing_unlock_when_no_scaffold():
    ctx = pb.BlockContext()
    msg = pb.format_spec_missing_block("STANDARD", "s1", "contract text", ctx=ctx)
    assert pb._RESTATE_LINE in msg
    assert pb._ADD_TASK_LINE in msg


def test_spec_missing_heavy_adds_primary_and_frontiers():
    ctx = pb.BlockContext()
    msg = pb.format_spec_missing_block("HEAVY", "s1", "contract text", ctx=ctx)
    assert pb._HEAVY_SET_PRIMARY_LINE in msg
    assert pb._HEAVY_ADD_FRONTIER_LINE in msg


def test_is_redundant_with_notify():
    unlock = "\n".join(("Next:", pb._RESTATE_LINE, pb._ADD_TASK_LINE))
    assert pb.is_redundant_with_notify(unlock, "Read foo.py first.")
    assert pb.is_redundant_with_notify("Read foo.py first.", "Read foo.py first.")
    assert not pb.is_redundant_with_notify("npm is not in the Bash research whitelist.", "Read foo.py first.")


def test_is_boilerplate_only():
    unlock = "\n".join(("Next:", pb._RESTATE_LINE, pb._ADD_TASK_LINE))
    assert pb.is_boilerplate_only(unlock)
    assert not pb.is_boilerplate_only("npm is not in the Bash research whitelist.")


def test_heavy_workflow_phase_hint():
    import heavy_workflow as hw

    hint = hw.heavy_workflow_phase_hint(phase="frontier")
    assert hint.strip()
    assert len(hint) < 200


def test_format_spec_validation_compact_when_scaffold_notified():
    from spec_contracts import format_spec_validation_block

    reasons = ["missing prior_art", "run unifable restate 'goal' first"]
    msg = format_spec_validation_block(
        "STANDARD",
        reasons,
        include_contract=False,
        scaffold_notified=True,
        contract_notified=True,
    )
    assert msg.strip()


def test_block_context_contract_notified():
    ctx = pb.BlockContext(scaffold_notified=True, contract_notified=True)
    assert ctx.contract_notified is True


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
