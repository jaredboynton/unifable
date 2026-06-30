#!/usr/bin/env python3
"""The provisional lift is silent to the model: lift_reason is an internal audit
field, and the only model-facing lift text is a terse scope label.

Regression for the verbose "Temporary lift: {reason} Allowed scope: {scope}.
Mutation tools stay available inside that scope." message. The release judge
prompt schema must mark lift_reason internal-only, and _provisional_lift_message
must emit only the terse scope (never the reason, never the boilerplate).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import breaker_prompts as bp  # noqa: E402
import breaker_runtime as br  # noqa: E402


def test_provisional_lift_message_omits_reason_and_boilerplate():
    reason = "The model is blocked for asserting the slowest phase without evidence."
    scope = "Run the search-only bench once and inspect timing."
    msg = br._provisional_lift_message(reason, scope)
    assert reason not in msg
    assert "Mutation tools stay available" not in msg
    assert "Temporary lift" not in msg
    assert "Allowed scope:" not in msg
    # The terse scope is the only content carried.
    assert scope in msg
    assert msg.startswith("Provisional lift granted.")


def test_provisional_lift_message_without_scope():
    msg = br._provisional_lift_message("some internal reason", "")
    assert "some internal reason" not in msg
    assert msg == "Provisional lift granted."


def test_lift_reason_schema_marked_internal_only():
    desc = str(bp._DISARM_SCHEMA["properties"]["lift_reason"]["description"])
    # The judge prompt must tell the judge that lift_reason is NOT shown to the
    # agent (internal audit note only), so it never writes model-facing prose there.
    assert "NOT shown to the agent" in desc
    assert "internal" in desc.lower()


def test_lift_scope_schema_asks_for_terse_label():
    desc = str(bp._DISARM_SCHEMA["properties"]["lift_scope"]["description"])
    # lift_scope is the ONLY lift field shown to the agent; it must be a terse
    # one-line label, not a paragraph of meta-reasoning.
    assert "terse" in desc.lower() or "one-line" in desc.lower()
    assert "shown to the agent" in desc


def test_lift_reason_still_required_for_event_log():
    # The field stays required so the judge still produces it for the LIFT event
    # log, even though it is no longer model-facing.
    assert "lift_reason" in bp._DISARM_SCHEMA["required"]
    assert "lift_scope" in bp._DISARM_SCHEMA["required"]


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
