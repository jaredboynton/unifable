"""Regression: the completion breaker must not run away.

Empirical origin: a Stop-hook runaway where the judge appended new requirements
faster than auto_validate drained them (AUTO_VALIDATE_MAX_TASKS=3 per cycle), so
the task list grew monotonically (observed 77 -> 85 -> ... -> 166 -> 176,
~+13/cycle) and the completion breaker -- which had NO stop-block cap -- blocked
Stop forever. Only the host's generic CLAUDE_CODE_STOP_HOOK_BLOCK_CAP (9) finally
overrode it, and that is Claude-Code-only.

These tests bound all three layers of the fix:
  1. dedup           -- a verbatim (title+check) duplicate is not re-added
  2. total cap       -- judge-added tasks per spec are hard-capped
  3. breaker release -- the completion breaker releases Stop after a bounded run
                        of stalled (no-net-progress) blocks (host-agnostic).
"""

from __future__ import annotations

import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    JUDGE_MAX_UNRESOLVED_ADDED,
    auto_validate_spec,
    load_spec,
    save_spec,
    spec_template,
)
from verify_state import (  # noqa: E402
    COMPLETION_MAX_STALLED_BLOCKS,
    note_completion_block,
    reset_completion_stall,
)


def _task(tid, status, check="true", title=None):
    return {"id": tid, "title": title or tid, "check": check, "status": status}


def _single_pending(tmp_path, monkeypatch, new_reqs, base_check="pytest -k base"):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending", check=base_check)]
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(
        spec_mod, "judge_task",
        lambda sp, t, ec, out: (1, "ok", [dict(r) for r in new_reqs], "", ""),
    )
    return load_spec(str(tmp_path), "K")


# --- layer 1: dedup ---------------------------------------------------------

def test_dedup_skips_verbatim_duplicate_requirement(tmp_path, monkeypatch):
    """A judge requirement byte-identical (title+check) to an existing task is
    not re-appended; a genuinely distinct one still is."""
    dup = {"title": "T1", "check": "pytest -k base"}
    fresh = {"title": "handle errors", "check": "pytest -k errors"}
    spec = _single_pending(tmp_path, monkeypatch, [dup, fresh])
    spec, _ = auto_validate_spec(spec, str(tmp_path))
    pairs = [(t["title"], t["check"]) for t in spec["tasks"]]
    assert pairs.count(("T1", "pytest -k base")) == 1  # duplicate refused
    assert ("handle errors", "pytest -k errors") in pairs  # distinct accepted


def test_dedup_preserves_distinct_title_sharing_trivial_check(tmp_path, monkeypatch):
    """A distinct requirement that happens to share a trivial check ("true")
    must still be added -- dedup keys on the (title, check) PAIR, not check
    alone, so it never over-suppresses (and the existing lifecycle test holds)."""
    spec = _single_pending(
        tmp_path, monkeypatch,
        [{"title": "also handle errors", "check": "true"}],
        base_check="true",
    )
    spec, _ = auto_validate_spec(spec, str(tmp_path))
    assert [t["id"] for t in spec["tasks"]] == ["T1", "T2"]
    assert spec["tasks"][1]["added_by"] == "judge"


# --- layer 2: total cap -----------------------------------------------------

def test_unresolved_judge_backlog_is_capped(tmp_path, monkeypatch):
    """Once the unresolved judge-added backlog hits JUDGE_MAX_UNRESOLVED_ADDED,
    no further judge requirement is appended."""
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending", check="pytest -k base")]
    for i in range(JUDGE_MAX_UNRESOLVED_ADDED):
        jt = _task(f"J{i}", "pending", check=f"pytest -k j{i}", title=f"j{i}")
        jt["added_by"] = "judge"
        s["tasks"].append(jt)
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(
        spec_mod, "judge_task",
        lambda sp, t, ec, out: (1, "ok", [{"title": "one more", "check": "pytest -k more"}], "", ""),
    )
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    judge_unresolved = sum(
        1 for t in spec["tasks"]
        if t.get("added_by") == "judge" and t.get("status") not in ("validated", "retracted", "rejected_approach")
    )
    assert judge_unresolved == JUDGE_MAX_UNRESOLVED_ADDED


# --- layer 3: breaker release ----------------------------------------------

def test_completion_breaker_releases_after_stalled_blocks():
    """Non-decreasing incomplete count (the runaway signature) trips the
    host-agnostic safety cap, releasing Stop instead of trapping the session."""
    led: dict = {}
    released = False
    for n in range(5, 5 + COMPLETION_MAX_STALLED_BLOCKS + 1):  # grows: 5,6,7,...
        released = note_completion_block(led, n)
    assert released is True
    assert int(led["completion_stall_blocks"]) >= COMPLETION_MAX_STALLED_BLOCKS


def test_completion_breaker_does_not_release_on_progress():
    """Strictly decreasing incomplete count (genuine convergence) never trips the
    cap, so a legitimate multi-cycle task is never released prematurely."""
    led: dict = {}
    for n in range(COMPLETION_MAX_STALLED_BLOCKS + 6, 0, -1):  # shrinks each block
        assert note_completion_block(led, n) is False


def test_reset_completion_stall_clears_counters():
    led = {"completion_stall_blocks": 4, "completion_prev_incomplete": 9}
    reset_completion_stall(led)
    assert int(led.get("completion_stall_blocks") or 0) == 0
    assert "completion_prev_incomplete" not in led
