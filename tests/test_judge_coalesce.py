#!/usr/bin/env python3
"""Coalescing of the groundedness judge across a parallel tool-call batch.

When the model fires N tool calls in parallel, the host spawns N concurrent
PreToolUse processes that share only the on-disk breaker file. Without
coordination each one fires its own gpt-realtime-2 judge call, all judging the
same transcript and returning the same verdict. These tests pin the fix:

  C1  coalesce=True skips every judge call but still blocks an armed mutation
  C2  evaluate_pre_tool_locked serializes a parallel batch -> exactly ONE judge call
  C3  a coalesced batch counts as ONE block toward the safety cap (not N)
  C4  calls spaced beyond the coalesce window are each judged (no over-suppression)
  C5  fail-open: with fcntl unavailable the locked path still arms/blocks

Run: python3 -m pytest tests/test_judge_coalesce.py -q
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import breaker_state as bs  # noqa: E402
import groundedness as gb  # noqa: E402
from breaker_state import default_breaker, load_breaker  # noqa: E402


class CountingJudge:
    """Routes arm/disarm/monitor like the real judges and counts every call.

    Sleeps briefly so the first caller holds the breaker lock long enough to
    force the rest of a parallel batch to contend for it."""

    def __init__(self, *, arm=(1, "blocked: prove it", "the cause is Y"), hold=0.05):
        self.arm_ret = arm
        self.hold = hold
        self.lock = threading.Lock()
        self.calls = 0

    def __call__(self, system, user, schema):
        with self.lock:
            self.calls += 1
        if self.hold:
            time.sleep(self.hold)
        low = system.lower()
        if "provisional-lift monitor" in low:
            return {"drift_level": 0, "feedback": ""}
        if "release monitor" in low:
            return {
                "grounded": 0, "needed": "read X and cite it", "load_bearing": 1,
                "provisional_release": 0, "lift_reason": "", "lift_scope": "",
            }
        v, s, c = self.arm_ret
        return {"verdict": v, "steering": s, "claim": c, "load_bearing": 1}


def _payload(session="batch", cwd="/repo"):
    return {"tool_name": "Edit", "session_id": session, "cwd": cwd}


# --- C1: the coalesce flag skips judging but preserves the block ----------------

def test_coalesce_true_skips_judge_but_still_blocks_when_armed(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    state = default_breaker()
    state["breaker_armed"] = True
    state["breaker_claim"] = "claim X"
    state["breaker_steering"] = "blocked: prove claim X"
    state["breaker_key"] = gb.breaker_key("S", "P")
    judge = CountingJudge(hold=0)
    blocked, steering, _ = gb.evaluate_pre_tool(
        _payload(session="S"), state, now=5.0, active_task="P", judge=judge, coalesce=True
    )
    assert blocked is True
    assert "blocked" in steering.lower()
    assert judge.calls == 0  # no API call when coalesced


def test_coalesce_true_does_not_increment_block_count(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    state = default_breaker()
    state["breaker_armed"] = True
    state["breaker_claim"] = "claim X"
    state["breaker_steering"] = "blocked"
    state["breaker_key"] = gb.breaker_key("S", "P")
    state["breaker_block_count"] = 1
    gb.evaluate_pre_tool(
        _payload(session="S"), state, now=5.0, active_task="P",
        judge=CountingJudge(hold=0), coalesce=True,
    )
    assert state["breaker_block_count"] == 1  # unchanged


# --- C2 + C3: a parallel batch makes one call and counts as one block -----------

def test_parallel_batch_makes_one_judge_call(monkeypatch, tmp_path):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "model: definitely the cause is Y")
    judge = CountingJudge()
    payload = _payload(session="parallel-1", cwd=str(tmp_path))

    results: list[tuple] = []
    rlock = threading.Lock()

    def one() -> None:
        out = gb.evaluate_pre_tool_locked(payload, time.time(), "P", judge=judge)
        with rlock:
            results.append(out)

    threads = [threading.Thread(target=one) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6
    assert judge.calls == 1, f"expected one judge call for the batch, got {judge.calls}"
    assert all(r[0] is True for r in results), "every mutation in the armed batch must block"
    # C3: the whole batch is one block toward the fail-open cap, not six.
    final = load_breaker(payload)
    assert final["breaker_block_count"] == 1


# --- C4: spacing beyond the window judges again ---------------------------------

def test_sequential_beyond_window_each_judges(monkeypatch, tmp_path):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = CountingJudge(hold=0)
    payload = _payload(session="seq", cwd=str(tmp_path))

    gb.evaluate_pre_tool_locked(payload, 1000.0, "P", judge=judge)  # arms (1 call)
    assert judge.calls == 1
    # far beyond the coalesce window, still armed -> the release judge fires again.
    gb.evaluate_pre_tool_locked(payload, 1000.0 + 600.0, "P", judge=judge)
    assert judge.calls == 2


# --- C5: fail-open when fcntl is unavailable ------------------------------------

def test_locked_path_arms_when_fcntl_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    monkeypatch.setattr(bs, "fcntl", None)
    judge = CountingJudge(hold=0)
    payload = _payload(session="no-fcntl", cwd=str(tmp_path))
    block, steering, notify, state = gb.evaluate_pre_tool_locked(payload, 10.0, "P", judge=judge)
    assert block is True
    assert state["breaker_armed"] is True


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
