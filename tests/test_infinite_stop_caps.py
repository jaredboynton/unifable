#!/usr/bin/env python3
"""The completion/goal Stop-block caps default to 0 == infinite.

The whole point of the gate is to keep a session looping until the work is
genuinely complete, so the shipped default is "no cap": the breaker never
auto-releases Stop on a block counter. The caps stay overridable via UNIFABLE_*
env vars (set in settings.json) for anyone who wants a finite escape hatch.

0 must mean "never auto-release", NOT "release immediately": every release site
compares `counter >= cap`, so an unguarded cap of 0 would fire on the very first
block. These tests pin both the infinite default and the env override, including
the >= 0 / malformed fallback.

Run: python3 -m pytest tests/test_infinite_stop_caps.py -q
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import verify_state as vs  # noqa: E402


def test_default_completion_caps_are_infinite(monkeypatch):
    monkeypatch.delenv("UNIFABLE_COMPLETION_MAX_STALLED_BLOCKS", raising=False)
    monkeypatch.delenv("UNIFABLE_COMPLETION_MAX_STOP_BLOCKS", raising=False)
    reloaded = importlib.reload(vs)
    try:
        assert reloaded.COMPLETION_MAX_STALLED_BLOCKS == 0
        assert reloaded.COMPLETION_MAX_STOP_BLOCKS == 0
    finally:
        importlib.reload(vs)


def test_infinite_caps_never_release(monkeypatch):
    """With the default caps, no block signature ever auto-releases Stop:
    a constant stall, a growing runaway, and a fluctuating count all loop."""
    monkeypatch.setattr(vs, "COMPLETION_MAX_STALLED_BLOCKS", 0)
    monkeypatch.setattr(vs, "COMPLETION_MAX_STOP_BLOCKS", 0)
    led_const: dict = {}
    assert not any(vs.note_completion_block(led_const, 8) for _ in range(500))
    led_grow: dict = {}
    assert not any(vs.note_completion_block(led_grow, n) for n in range(5, 505))
    led_flux: dict = {}
    assert not any(vs.note_completion_block(led_flux, c) for c in [8, 7] * 100)


def test_env_override_restores_finite_cap(monkeypatch):
    monkeypatch.setenv("UNIFABLE_COMPLETION_MAX_STALLED_BLOCKS", "5")
    monkeypatch.setenv("UNIFABLE_COMPLETION_MAX_STOP_BLOCKS", "9")
    reloaded = importlib.reload(vs)
    try:
        assert reloaded.COMPLETION_MAX_STALLED_BLOCKS == 5
        assert reloaded.COMPLETION_MAX_STOP_BLOCKS == 9
        led: dict = {}
        released = [reloaded.note_completion_block(led, 8) for _ in range(6)]
        # Stalled signature -> release exactly at the 5th block, not before.
        assert released == [False, False, False, False, True, True]
    finally:
        monkeypatch.delenv("UNIFABLE_COMPLETION_MAX_STALLED_BLOCKS", raising=False)
        monkeypatch.delenv("UNIFABLE_COMPLETION_MAX_STOP_BLOCKS", raising=False)
        importlib.reload(vs)


def test_malformed_or_negative_env_falls_back_to_infinite(monkeypatch):
    try:
        for bad in ("abc", "-4", "", "  "):
            monkeypatch.setenv("UNIFABLE_COMPLETION_MAX_STALLED_BLOCKS", bad)
            reloaded = importlib.reload(vs)
            assert reloaded.COMPLETION_MAX_STALLED_BLOCKS == 0, f"bad={bad!r}"
    finally:
        monkeypatch.delenv("UNIFABLE_COMPLETION_MAX_STALLED_BLOCKS", raising=False)
        importlib.reload(vs)


def test_handoff_cap_defaults_infinite(monkeypatch):
    monkeypatch.delenv("UNIFABLE_COMPLETION_HANDOFF_BLOCK_CAP", raising=False)
    import completion_handoff

    reloaded = importlib.reload(completion_handoff)
    try:
        assert reloaded.COMPLETION_HANDOFF_BLOCK_CAP == 0
    finally:
        importlib.reload(completion_handoff)


def test_goal_stop_cap_defaults_infinite():
    # GOAL_STOP_BLOCK_CAP is read at import; the test environment sets no
    # override, so the shipped default must be 0 (infinite).
    import gate_stop

    assert gate_stop.GOAL_STOP_BLOCK_CAP == 0


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
