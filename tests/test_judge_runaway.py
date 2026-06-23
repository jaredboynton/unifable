"""Regression: the completion breaker must not run away.

Empirical origin: a Stop-hook runaway where the judge appended new requirements
faster than auto_validate drained them (AUTO_VALIDATE_MAX_TASKS=3 per cycle), so
the task list grew monotonically (observed 77 -> 85 -> ... -> 166 -> 176,
~+13/cycle) and the completion breaker -- which had NO stop-block cap -- blocked
Stop forever. Only the host's generic CLAUDE_CODE_STOP_HOOK_BLOCK_CAP (9) finally
overrode it, and that is Claude-Code-only.

These tests bound all four layers of the fix:
  1. dedup            -- a verbatim (title+check) duplicate is not re-added
  2. backlog cap      -- the UNRESOLVED judge-added backlog is capped (live, not
                         lifetime: resolved judge tasks free up slots)
  3. judge self-adjust-- the judge may retract/revise its OWN requirements and
                         tell the main model, instead of re-adding equivalents
  4. breaker release  -- the completion breaker releases Stop after a bounded run
                         of stalled (no-net-progress) blocks (host-agnostic).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    JUDGE_MAX_UNRESOLVED_ADDED,
    _apply_adjustments,
    _judge_user,
    all_tasks_validated,
    auto_validate_spec,
    judge_task,
    load_spec,
    save_spec,
    spec_template,
)
from verify_state import (  # noqa: E402
    COMPLETION_MAX_STALLED_BLOCKS,
    note_completion_block,
    reset_completion_stall,
)


def _task(tid, status, check="true", title=None, added_by=None):
    t = {"id": tid, "title": title or tid, "check": check, "status": status}
    if added_by:
        t["added_by"] = added_by
    return t


def _single_pending(tmp_path, monkeypatch, new_reqs, base_check="pytest -k base", extra=None):
    """One pending agent task -> auto_validate judges it in the single-task path,
    so the mocked judge_task applies deterministically (no batch/network)."""
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending", check=base_check)] + list(extra or [])
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(
        spec_mod, "judge_task",
        lambda sp, t, ec, out: (1, "ok", [dict(r) for r in new_reqs], "", ""),
    )
    return load_spec(str(tmp_path), "K")


def _unresolved_judge(spec):
    return [t for t in spec["tasks"]
            if t.get("added_by") == "judge"
            and t.get("status") not in ("validated", "retracted", "rejected_approach")]


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


# --- layer 2: unresolved-backlog cap ---------------------------------------

def test_unresolved_judge_backlog_is_capped(tmp_path, monkeypatch):
    """When the judge tries to add more distinct requirements in one cycle than
    the cap allows, only JUDGE_MAX_UNRESOLVED_ADDED are appended."""
    many = [{"title": f"req{i}", "check": f"pytest -k r{i}"}
            for i in range(JUDGE_MAX_UNRESOLVED_ADDED + 5)]
    spec = _single_pending(tmp_path, monkeypatch, many)
    spec, _ = auto_validate_spec(spec, str(tmp_path))
    assert len(_unresolved_judge(spec)) == JUDGE_MAX_UNRESOLVED_ADDED  # backlog bounded


def test_resolved_judge_tasks_free_backlog_slots(tmp_path, monkeypatch):
    """Resolved judge tasks do NOT count against the cap -- a long task that
    validates old judge requirements keeps room for new ones."""
    resolved = [
        _task(f"J{i}", "validated", check=f"pytest -k j{i}", title=f"j{i}", added_by="judge")
        for i in range(JUDGE_MAX_UNRESOLVED_ADDED)
    ]
    spec = _single_pending(
        tmp_path, monkeypatch,
        [{"title": "fresh", "check": "pytest -k fresh"}],
        extra=resolved,
    )
    spec, _ = auto_validate_spec(spec, str(tmp_path))
    assert any(t["title"] == "fresh" and t.get("added_by") == "judge" for t in spec["tasks"])


# --- layer 3: judge self-adjust (retract / revise) -------------------------

def test_adjust_retracts_judge_own_requirement():
    spec = {"tasks": [
        _task("T1", "pending", added_by="agent"),
        _task("T2", "pending", check="brittle", added_by="judge"),
    ]}
    res = {"verdict": 1, "reason": "ok",
           "adjust_requirements": [{"id": "T2", "action": "retract", "reason": "duplicate of T1"}]}
    headlines = _apply_adjustments(spec, res)
    t2 = next(t for t in spec["tasks"] if t["id"] == "T2")
    assert t2["status"] == "retracted"
    assert any("retracted T2" in h for h in headlines)


def test_adjust_revises_check_and_reopens_for_validation():
    spec = {"tasks": [
        _task("T2", "validated", check="grep 'two parallel judge models' README.md", added_by="judge"),
    ]}
    res = {"adjust_requirements": [{
        "id": "T2", "action": "revise", "reason": "literal phrase is factually wrong",
        "check": "grep 'symbiotic' README.md",
    }]}
    _apply_adjustments(spec, res)
    t2 = spec["tasks"][0]
    assert t2["check"] == "grep 'symbiotic' README.md"
    assert t2["status"] == "pending"  # re-opened for re-validation
    assert t2["judge_verdict"] is None


def test_adjust_never_touches_agent_requirements():
    """The judge may only adjust its OWN requirements, never the agent's."""
    spec = {"tasks": [_task("T1", "pending", added_by="agent")]}
    res = {"adjust_requirements": [{"id": "T1", "action": "retract", "reason": "want it gone"}]}
    headlines = _apply_adjustments(spec, res)
    assert spec["tasks"][0]["status"] == "pending"  # untouched
    assert headlines == []


def test_adjust_skips_task_being_judged_this_cycle():
    spec = {"tasks": [_task("T2", "pending", added_by="judge")]}
    res = {"adjust_requirements": [{"id": "T2", "action": "retract", "reason": "x"}]}
    _apply_adjustments(spec, res, skip_ids={"T2"})
    assert spec["tasks"][0]["status"] == "pending"  # not adjusted while being judged


def test_judge_added_current_task_can_self_retract(monkeypatch):
    spec = {"restated_goal": "g", "tasks": [
        _task("T1", "validated", added_by="agent"),
        _task("T2", "failed", check="true", added_by="judge"),
    ]}

    import codex_judge

    def fake_ask(system, user, schema, schema_name):
        return {
            "verdict": 0,
            "reason": "T2 duplicates validated T1",
            "new_requirements": [],
            "adjust_requirements": [
                {"id": "T2", "action": "retract", "reason": "duplicate of validated T1"}
            ],
        }

    monkeypatch.setattr(codex_judge, "ask_structured", fake_ask)
    verdict, reason, new_reqs, hint, frontier_outcome = judge_task(spec, spec["tasks"][1], 0, "ok")
    assert verdict == 0
    assert spec["tasks"][1]["status"] == "retracted"
    assert "duplicates" in reason
    assert new_reqs == []
    assert hint == ""
    assert frontier_outcome == ""


# --- layer 4: breaker release ----------------------------------------------

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


def test_stall_counters_survive_ledger_roundtrip(tmp_path, monkeypatch):
    """The stall counters MUST be registered in DEFAULT_LEDGER. load_ledger rebuilds
    the ledger from DEFAULT_LEDGER's keys and DROPS unknown keys, so an unregistered
    counter resets to 0 every stop and the stall-release backstop never accumulates
    to its cap (the backstop would be silently dead). Round-trip proves persistence."""
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    from ledger import DEFAULT_LEDGER, load_ledger, save_ledger
    assert "completion_stall_blocks" in DEFAULT_LEDGER
    assert "completion_prev_incomplete" in DEFAULT_LEDGER
    inp = {"session_id": "stall-test", "cwd": str(tmp_path)}
    led = load_ledger(inp)
    # Simulate two consecutive no-progress blocks, then persist + reload.
    note_completion_block(led, 6)
    note_completion_block(led, 6)
    save_ledger(inp, led)
    reloaded = load_ledger(inp)
    assert reloaded["completion_stall_blocks"] == 2          # survived the round-trip
    assert reloaded["completion_prev_incomplete"] == 6
    # And a third no-progress block keeps accumulating (it would reset to 1 if dropped).
    assert note_completion_block(reloaded, 6) is (3 >= COMPLETION_MAX_STALLED_BLOCKS)
    assert reloaded["completion_stall_blocks"] == 3


# --- layer 0: judge sees ALL requirements (root-cause fix) ------------------

def test_judge_user_lists_all_requirements():
    """The per-task judge payload must expose EVERY current requirement (agent +
    validated), not only judge-added ones, so the judge can tell a goal aspect is
    already covered and not re-derive it. Root cause of the redundancy loop: the
    agent's own requirements were invisible to the judge."""
    spec = {"restated_goal": "g", "tasks": [
        _task("T1", "validated", check="pytest -k a", title="already covered thing", added_by="agent"),
        _task("T2", "pending", check="pytest -k b", title="being judged", added_by="agent"),
    ]}
    payload = json.loads(_judge_user(spec, spec["tasks"][1], 0, "ok"))
    current = payload.get("current_requirements") or []
    titles = {r.get("title") for r in current}
    ids = {r.get("id") for r in current}
    assert "already covered thing" in titles  # the validated AGENT task is visible
    assert "T1" in ids


def test_dedup_drops_reworded_title_duplicate(tmp_path, monkeypatch):
    """A new requirement whose NORMALIZED title matches an existing task's
    (case / whitespace / trailing parenthetical only) is dropped, even when its
    check differs so the byte-identical pair check would miss it. A genuinely
    distinct title is still added."""
    reworded = {"title": "  Handle Errors  ", "check": "pytest -k diff"}  # vs existing "handle errors"
    distinct = {"title": "validate timeouts", "check": "pytest -k to"}
    spec = _single_pending(
        tmp_path, monkeypatch, [reworded, distinct],
        base_check="pytest -k base",
        extra=[_task("E1", "validated", check="pytest -k e", title="handle errors", added_by="agent")],
    )
    spec, _ = auto_validate_spec(spec, str(tmp_path))
    norm = [str(t["title"]).strip().lower() for t in spec["tasks"]]
    assert norm.count("handle errors") == 1  # reworded duplicate refused
    assert any(t["title"] == "validate timeouts" for t in spec["tasks"])  # distinct kept


def test_no_churn_when_judge_sees_existing(tmp_path, monkeypatch):
    """End-to-end: once the judge can see a covering requirement in
    current_requirements it stops re-deriving it, so auto_validate converges and the
    breaker opens. Fails today because _judge_user hides agent/validated tasks, so
    the (distinct-check) re-derivation is appended and the breaker stays closed."""
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "no judge/hint duplication"
    s["tasks"] = [
        _task("T1", "validated", check="pytest -k cover", title="no duplicate judge or hint", added_by="agent"),
        _task("T2", "pending", check="pytest -k other", title="other requirement", added_by="agent"),
    ]
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))

    def judge_via_payload(sp, t, ec, out):
        payload = json.loads(_judge_user(sp, t, ec, out))
        titles = {str(r.get("title") or "").strip().lower()
                  for r in payload.get("current_requirements") or []}
        # Re-derive the covering requirement (distinct check) unless already visible.
        want = {"title": "no duplicate judge or hint", "check": "pytest -k rederived"}
        new = [] if want["title"].lower() in titles else [want]
        return (1, "ok", new, "", "")
    monkeypatch.setattr(spec_mod, "judge_task", judge_via_payload)

    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert all_tasks_validated(spec)[0] is True  # breaker opens
    assert [t for t in spec["tasks"] if t.get("added_by") == "judge"] == []  # nothing re-added
