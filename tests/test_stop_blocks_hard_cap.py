#!/usr/bin/env python3
"""Failing-first tests for the completion_stop_blocks hard-cap release trigger.

These prove the fix for the trap where all release signals reset when the
incomplete task set fluctuates (count bounces 8->7->8, set identity changes),
leaving only completion_stop_blocks climbing -- but no release path read it.

  H1  Hard cap: note_completion_block releases after COMPLETION_MAX_STOP_BLOCKS
      raw blocks even when the count-based streak resets on every fluctuation.
  H2  note_completion_block owns the stop counter (bumps it exactly once per
      call); _completion_stop_hint no longer mutates completion_stop_blocks.
  H3  reset_completion_stall zeroes completion_stop_blocks.
  H4  judge_completion_loop_release payload includes completion_stop_blocks as a
      top-level field with a hard-cap context string.
  H5  should_invoke_loop_judge re-fires past a re-judge threshold on
      completion_stop_blocks even if the episode was already judged.

Run: python3 -m pytest tests/test_stop_blocks_hard_cap.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import loop_release as lr  # noqa: E402
import verify_state as vs  # noqa: E402
from verify_state import (  # noqa: E402
    note_completion_block,
    reset_completion_stall,
)

# The shipped default caps are now 0 (infinite). This whole suite proves the
# hard-cap RELEASE behavior, which only exists when the caps are finite, so pin
# the historical finite values for every test here.
COMPLETION_MAX_STALLED_BLOCKS = 6
COMPLETION_MAX_STOP_BLOCKS = 12


@pytest.fixture(autouse=True)
def _finite_completion_caps(monkeypatch):
    monkeypatch.setattr(vs, "COMPLETION_MAX_STALLED_BLOCKS", COMPLETION_MAX_STALLED_BLOCKS)
    monkeypatch.setattr(vs, "COMPLETION_MAX_STOP_BLOCKS", COMPLETION_MAX_STOP_BLOCKS)


# H1 -------------------------------------------------------------------------
def test_hard_cap_releases_on_fluctuation():
    """The exact transcript scenario: count bounces 8->7->8->7 for 15 cycles.
    The count-based streak resets constantly, but the raw stop-block counter
    must still trigger a hard release at COMPLETION_MAX_STOP_BLOCKS."""
    led = {}
    counts = [8, 7] * 8  # 16 cycles of fluctuation
    released_at = None
    for i, count in enumerate(counts):
        released = note_completion_block(led, count)
        if released and released_at is None:
            released_at = i + 1
        if released:
            break
    assert released_at is not None, "hard cap never fired despite 15+ blocked stops"
    # Must fire at exactly COMPLETION_MAX_STOP_BLOCKS (not earlier, not never)
    assert led["completion_stop_blocks"] == COMPLETION_MAX_STOP_BLOCKS
    # The count-based streak was broken by fluctuation, proving the hard cap
    # is the mechanism that saved the session, not the streak.
    assert led["completion_stall_blocks"] < COMPLETION_MAX_STALLED_BLOCKS


# H2 -------------------------------------------------------------------------
def test_note_completion_block_owns_stop_counter():
    """note_completion_block must bump completion_stop_blocks exactly once per
    call (not zero, not twice)."""
    led = {}
    note_completion_block(led, 5)
    assert led["completion_stop_blocks"] == 1
    note_completion_block(led, 5)
    note_completion_block(led, 5)
    assert led["completion_stop_blocks"] == 3


def test_completion_stop_hint_no_longer_mutates_counter(monkeypatch, tmp_path):
    """_completion_stop_hint must not bump completion_stop_blocks (that ownership
    moved to note_completion_block). It reads the counter for its own threshold
    logic but never writes it."""
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    payload = {"session_id": "hint-test", "cwd": str(tmp_path)}
    from ledger import save_ledger

    save_ledger(payload, {"completion_stop_blocks": 2})

    # Patch judge_hint so no network call happens; the hint path is exercised.
    monkeypatch.setattr("spec.judge_hint", lambda spec, **kw: "nudge")
    spec = {"restated_goal": "test", "tasks": []}
    hint = gate_stop._completion_stop_hint(payload, spec, ["T1"])
    # The hint may or may not fire depending on threshold, but either way the
    # counter must NOT have been bumped by the hint function.
    from ledger import load_ledger

    reloaded = load_ledger(payload)
    assert reloaded["completion_stop_blocks"] == 2, "_completion_stop_hint must not mutate completion_stop_blocks"


# H3 -------------------------------------------------------------------------
def test_reset_clears_stop_blocks():
    """reset_completion_stall must zero completion_stop_blocks,
    completion_best_incomplete, and completion_prev_incomplete so the counter
    resets on genuine open."""
    led = {
        "completion_stall_blocks": 4,
        "completion_stop_blocks": 10,
        "completion_prev_incomplete": 8,
        "completion_best_incomplete": 7,
    }
    reset_completion_stall(led)
    assert led["completion_stall_blocks"] == 0
    assert led["completion_stop_blocks"] == 0
    assert "completion_prev_incomplete" not in led
    assert "completion_best_incomplete" not in led


# H4 -------------------------------------------------------------------------
def test_loop_judge_payload_includes_stop_blocks_with_context(monkeypatch):
    """The judge payload must include completion_stop_blocks as a top-level
    field AND a hard_cap field so the judge can weigh the raw signal."""

    captured = {}

    class FakeAsk:
        def __call__(self, system, user, schema, schema_name=""):
            import json as _json

            captured["payload"] = _json.loads(user)
            return {"suicide_loop": False, "lift": "none", "reason": "test"}

    monkeypatch.setattr("codex_judge.ask_structured", FakeAsk())
    spec = {"restated_goal": "g", "tasks": []}
    led = {"completion_stop_blocks": 15, "completion_stall_blocks": 1}
    lr.judge_completion_loop_release(spec, led, signal="stuck")
    payload = captured["payload"]
    assert payload["completion_stop_blocks"] == 15
    assert "hard_cap" in payload
    assert payload["hard_cap"] == COMPLETION_MAX_STOP_BLOCKS


# H5 -------------------------------------------------------------------------
def test_should_invoke_loop_judge_re_fires_past_threshold():
    """After the judge declined once for an episode, it should re-fire when
    completion_stop_blocks crosses a re-judge threshold (not stay suppressed
    forever by loop_judge_episode_id)."""
    led = {
        "completion_stop_blocks": 12,  # past the re-judge threshold
        "loop_episode_id": "T1",
        "loop_judge_episode_id": "T1",  # already judged this episode
        "loop_judge_at_stop_blocks": 6,  # judged when count was 6; now 12 (+6 >= step=4)
        "loop_judge_last_at": 0.0,  # no debounce
        "completion_stall_blocks": 1,  # weak streak (fluctuating)
        "loop_same_set_streak": 1,
    }
    # stall_signature fires (stop_blocks >= threshold), but the episode guard
    # would normally suppress re-judge. The re-fire threshold must override it.
    assert lr.should_invoke_loop_judge(led, ["T1"], pending_block=True) is True


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
