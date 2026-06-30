#!/usr/bin/env python3
"""Scaffold onboarding must not re-emit the "unifable restate" instruction when
the SessionStart frame already fired (Redundancy-1).

SessionStart injects "FIRST ACTION REQUIRED: ... unifable restate '<goal>'".
The first-prompt scaffold onboarding previously repeated that as "1. unifable
restate ...". When session_frame_notified is set, the scaffold must start at
add-task instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import gate_prompt  # noqa: E402


def test_scaffold_omits_restate_when_session_frame_fired():
    block = gate_prompt._format_scaffold_onboarding(
        "/tmp/spec.json",
        evidence_profile="code",
        heavy_scaffold=False,
        plan_mode={},
        session_frame_fired=True,
    )
    assert "unifable restate" not in block
    # Onboarding starts at add-task (renumbered to 1).
    assert "1. unifable add-task" in block
    assert "2. unifable add-task" not in block


def test_scaffold_includes_restate_when_no_session_frame():
    block = gate_prompt._format_scaffold_onboarding(
        "/tmp/spec.json",
        evidence_profile="code",
        heavy_scaffold=False,
        plan_mode={},
        session_frame_fired=False,
    )
    assert "1. unifable restate" in block
    assert "2. unifable add-task" in block


def test_scaffold_heavy_branch_omits_restate_when_frame_fired():
    block = gate_prompt._format_scaffold_onboarding(
        "/tmp/spec.json",
        evidence_profile="code",
        heavy_scaffold=True,
        plan_mode={},
        session_frame_fired=True,
    )
    assert "unifable restate" not in block
    assert "unifable add-task" in block
    # HEAVY addendum is independent of the restate de-dup.
    assert "unifable set-primary" in block


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
