#!/usr/bin/env python3
"""The standing groundedness-breaker status line must be actionable.

Regression for the truncated, next-step-free "breaker: ARMED on '<60char>'"
line: every armed/provisional status must state why the breaker is engaged and
the exact next step to clear it (reusing breaker_steering/directive), and must
never cut the claim mid-word.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import breaker_state  # noqa: E402
import gate_post_tool  # noqa: E402


def test_armed_status_carries_steering_next_step(monkeypatch):
    steering = (
        "You asserted the auth middleware rejects expired tokens. Read "
        "src/auth/middleware.py and run pytest tests/test_auth.py to ground it."
    )
    monkeypatch.setattr(
        breaker_state,
        "load_breaker",
        lambda inp: {
            "breaker_armed": True,
            "breaker_claim": "the auth middleware rejects expired tokens",
            "breaker_steering": steering,
        },
    )
    out = gate_post_tool._breaker_status_context({})
    assert out.startswith("breaker: ARMED")
    assert "the auth middleware rejects expired tokens" in out
    # The actionable steering is surfaced verbatim, not dropped.
    assert steering in out
    assert "To disarm:" in out


def test_armed_status_falls_back_to_directive(monkeypatch):
    monkeypatch.setattr(
        breaker_state,
        "load_breaker",
        lambda inp: {
            "breaker_armed": True,
            "breaker_claim": "the cache invalidates on write",
            "breaker_steering": "",
            "breaker_directive": "Read cache.py around the write path.",
        },
    )
    out = gate_post_tool._breaker_status_context({})
    assert "To disarm: Read cache.py around the write path." in out


def test_armed_status_generic_fallback_without_steering(monkeypatch):
    monkeypatch.setattr(
        breaker_state,
        "load_breaker",
        lambda inp: {
            "breaker_armed": True,
            "breaker_claim": "the build passes",
            "breaker_steering": "",
            "breaker_directive": "",
        },
    )
    out = gate_post_tool._breaker_status_context({})
    assert out.startswith("breaker: ARMED on 'the build passes'")
    # Still actionable even when no steering was stored.
    assert "To disarm:" in out
    assert "evidence that proves the claim" in out


def test_armed_status_does_not_truncate_claim_midword(monkeypatch):
    # A long claim must end on a whole word + ellipsis, never a partial word
    # like the original "...sole model-facing not".
    claim = (
        "The transcript asserts that there is a sole model-facing notification "
        "path and that every other emitter routes through it without exception"
    )
    monkeypatch.setattr(
        breaker_state,
        "load_breaker",
        lambda inp: {"breaker_armed": True, "breaker_claim": claim, "breaker_steering": ""},
    )
    out = gate_post_tool._breaker_status_context({})
    # Extract the quoted claim segment.
    start = out.index("'") + 1
    end = out.index("'", start)
    rendered = out[start:end]
    assert rendered  # non-empty
    # Either the full claim, or a word-boundary clip ending in the ellipsis.
    if rendered != claim:
        assert rendered.endswith("...")
        body = rendered[: -len("...")].strip()
        # Every rendered token is a real, whole token from the source claim.
        claim_tokens = set(claim.split())
        assert all(tok in claim_tokens for tok in body.split())


def test_provisional_status_states_scope_and_reason(monkeypatch):
    monkeypatch.setattr(
        breaker_state,
        "load_breaker",
        lambda inp: {
            "breaker_armed": False,
            "breaker_provisional": True,
            "breaker_lift_scope": "edits under src/parser/",
            "breaker_lift_reason": "you are actively reading the cited parser source",
        },
    )
    out = gate_post_tool._breaker_status_context({})
    assert out.startswith("breaker: PROVISIONAL")
    assert "edits under src/parser/" in out
    assert "you are actively reading the cited parser source" in out
    assert "re-arms" in out
