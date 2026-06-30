#!/usr/bin/env python3
"""PostToolUse no longer narrates standing breaker state to the model.

The standing "breaker: ARMED on '...'" / "breaker: PROVISIONAL lift (...)"
line was redundant with the PreToolUse one-shot lift/block notify (which
arrives at a moment the model can act on it) and re-narrated judge prose
the model should not see. PostToolUse does not gate, so the standing line
is purely advisory and is now intentionally empty. The PreToolUse one-shot
notify is the single source of breaker guidance.

This regression test pins the new contract: _breaker_status_context returns
"" for armed, provisional, and disarmed states alike.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import breaker_state  # noqa: E402
import gate_post_tool  # noqa: E402


def test_armed_status_is_silent(monkeypatch):
    monkeypatch.setattr(
        breaker_state,
        "load_breaker",
        lambda inp: {
            "breaker_armed": True,
            "breaker_claim": "the auth middleware rejects expired tokens",
            "breaker_steering": "Read src/auth/middleware.py and run pytest tests/test_auth.py.",
        },
    )
    assert gate_post_tool._breaker_status_context({}) == ""


def test_armed_status_silent_without_steering(monkeypatch):
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
    assert gate_post_tool._breaker_status_context({}) == ""


def test_provisional_status_is_silent(monkeypatch):
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
    assert gate_post_tool._breaker_status_context({}) == ""


def test_disarmed_status_is_silent(monkeypatch):
    monkeypatch.setattr(
        breaker_state,
        "load_breaker",
        lambda inp: {"breaker_armed": False, "breaker_provisional": False},
    )
    assert gate_post_tool._breaker_status_context({}) == ""


def test_breaker_status_never_leaks_into_posttool_additional_context(monkeypatch):
    """The fold-in site must not inject a breaker line even when armed."""
    monkeypatch.setattr(
        breaker_state,
        "load_breaker",
        lambda inp: {
            "breaker_armed": True,
            "breaker_claim": "the cache invalidates on write",
            "breaker_steering": "Read cache.py around the write path.",
        },
    )
    assert gate_post_tool._breaker_status_context({}) == ""


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
