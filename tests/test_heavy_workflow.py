#!/usr/bin/env python3
"""Tests for HEAVY frontier-first approach workflow."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import heavy_workflow as hw  # noqa: E402
import spec_stop_validate as ssv  # noqa: E402
from spec import (  # noqa: E402
    all_tasks_validated,
    append_frontier_task,
    judge_discover_frontiers,
    load_spec,
    save_spec,
    set_primary_task,
    spec_template,
    validate_spec,
)
from spec_cli import (
    _cmd_add_frontier,  # noqa: E402
    _cmd_restate,
    _cmd_set_primary,
)


def _heavy_spec(tmp_path):
    spec = spec_template()
    spec["heavy_workflow"] = True
    spec["requires_tasks"] = True
    spec["restated_goal"] = "Ship the feature with evidence"
    spec["goal_seeded"] = False
    spec["acceptance_criteria"] = []
    spec["repo_context"] = [{"cite": "src/a.py:1", "why": "read this session"}]
    spec["prior_art"] = [{"cite": "https://example.com/doc", "why": "fetched this session"}]
    spec["tasks"] = []
    append_frontier_task(spec, "Streaming tokenizer path", "pytest tests/test_stream.py -q")
    append_frontier_task(spec, "SIMD batch decode", "pytest tests/test_simd.py -q")
    set_primary_task(spec, "Incremental byte buffer parser", "pytest tests/test_parser.py -q")
    save_spec(str(tmp_path), "K", spec)
    return load_spec(str(tmp_path), "K")


def test_phase_transitions():
    spec = spec_template()
    spec["restated_goal"] = "goal"
    assert hw.compute_heavy_phase(spec) == "declare"
    append_frontier_task(spec, "F1", "true")
    append_frontier_task(spec, "F2", "true")
    set_primary_task(spec, "Primary", "true")
    assert hw.compute_heavy_phase(spec) == "frontier"
    for t in hw.frontier_tasks(spec):
        t["status"] = "rejected_approach"
    hw.advance_primary_if_ready(spec)
    assert hw.compute_heavy_phase(spec) == "primary"


def test_phase_transitions_adoption():
    """Adoption path: accepted frontier -> adopted phase."""
    spec = spec_template()
    spec["restated_goal"] = "goal"
    append_frontier_task(spec, "F1", "true")
    append_frontier_task(spec, "F2", "true")
    set_primary_task(spec, "Primary", "true")
    f1, f2 = hw.frontier_tasks(spec)
    f1["status"] = "accepted_approach"
    f1["comparison_winner"] = True
    f2["status"] = "rejected_approach"
    assert hw.compute_heavy_phase(spec) == "adopted"


def test_heavy_validate_no_constraints(tmp_path):
    spec = _heavy_spec(tmp_path)
    ok, reasons = validate_spec(spec, "HEAVY", require_evidence=True)
    assert ok, reasons


def test_all_tasks_validated_heavy_completion(tmp_path):
    spec = _heavy_spec(tmp_path)
    for t in hw.frontier_tasks(spec):
        t["status"] = "rejected_approach"
    hw.advance_primary_if_ready(spec)
    primary = hw.primary_task(spec)
    assert primary is not None
    primary["status"] = "validated"
    ok, incomplete = all_tasks_validated(spec)
    assert ok, incomplete


def test_heavy_completion_adopted_frontier_with_validated_primary(tmp_path):
    """Regression: an adopted frontier (comparison_winner) plus a primary that was
    validated directly (not auto-superseded) must count as complete. The winner branch
    must use task_is_resolved, not hardcode 'superseded' for the primary."""
    spec = _heavy_spec(tmp_path)
    f1, f2 = hw.frontier_tasks(spec)
    f1["status"] = "validated"
    f1["comparison_winner"] = True
    f2["status"] = "rejected_approach"
    primary = hw.primary_task(spec)
    primary["status"] = "validated"
    ok, incomplete = all_tasks_validated(spec)
    assert ok, incomplete


def test_clear_stale_heavy_flag_for_standard_without_approach_tasks():
    spec = spec_template()
    spec["heavy_workflow"] = True
    spec["heavy_phase"] = "declare"
    spec["requires_tasks"] = True
    spec["tasks"] = [{"id": "T1", "title": "T1", "check": "true", "status": "validated"}]

    assert hw.clear_stale_heavy_workflow(spec, "STANDARD") is True
    assert spec["heavy_workflow"] is False
    assert "heavy_phase" not in spec
    assert all_tasks_validated(spec) == (True, [])


def test_clear_stale_heavy_flag_preserves_genuine_approach_tasks(tmp_path):
    spec = _heavy_spec(tmp_path)

    assert hw.clear_stale_heavy_workflow(spec, "STANDARD") is False
    assert spec["heavy_workflow"] is True
    assert len(hw.frontier_tasks(spec)) == 2
    assert hw.primary_task(spec) is not None


def test_cli_set_primary_and_add_frontier(tmp_path):
    save_spec(str(tmp_path), "K", spec_template())
    _cmd_restate(SimpleNamespace(root=str(tmp_path), task_id="K", goal="Build auth middleware"))
    _cmd_add_frontier(
        SimpleNamespace(
            root=str(tmp_path),
            task_id="K",
            title="JWT with rotation",
            check="pytest tests/test_jwt.py",
        )
    )
    _cmd_add_frontier(
        SimpleNamespace(
            root=str(tmp_path),
            task_id="K",
            title="Session cookies hardened",
            check="pytest tests/test_sess.py",
        )
    )
    rc = _cmd_set_primary(
        SimpleNamespace(
            root=str(tmp_path),
            task_id="K",
            title="HMAC bearer tokens",
            check="pytest tests/test_hmac.py",
        )
    )
    assert rc == 0
    spec = load_spec(str(tmp_path), "K")
    assert len(hw.frontier_tasks(spec)) == 2
    assert hw.primary_task(spec)["status"] == "blocked"


def test_judge_discover_frontiers_appends(monkeypatch):
    spec = spec_template()
    spec["restated_goal"] = "Optimize parser"
    spec["heavy_workflow"] = True

    import codex_judge

    def fake_ask(system, user, schema, schema_name):
        return {
            "frontiers": [
                {"title": "Zero-copy mmap", "check": "pytest tests/test_mmap.py", "scope_paths": ["src/parser.py"]},
            ],
            "reason": "mmap avoids copies",
        }

    monkeypatch.setattr(codex_judge, "ask_structured", fake_ask)
    added = judge_discover_frontiers(spec, {"read_paths": ["/x/src/parser.py"], "fetched_urls": []})
    assert len(added) == 1
    assert added[0]["approach_kind"] == "frontier"
    assert added[0]["added_by"] == "judge"


def test_frontier_judge_rejected_approach(tmp_path, monkeypatch):
    spec = _heavy_spec(tmp_path)
    frontier = hw.frontier_tasks(spec)[0]

    def fake_judge(sp, task, ec, out, **kw):
        return 0, "broken boundary: latency regression", [], "rejected_approach"

    monkeypatch.setattr(ssv, "judge_task", fake_judge)
    from spec_stop_validate import _validate_one_task

    _validate_one_task(spec, frontier, str(tmp_path))
    assert frontier["status"] == "rejected_approach"


def test_primary_blocked_until_frontiers_rejected(tmp_path):
    spec = _heavy_spec(tmp_path)
    assert hw.compute_heavy_phase(spec) == "frontier"
    primary = hw.primary_task(spec)
    assert primary["status"] == "blocked"
    for t in hw.frontier_tasks(spec):
        t["status"] = "rejected_approach"
    hw.advance_primary_if_ready(spec)
    assert primary["status"] == "pending"


def test_retracted_frontier_unlocks_primary(tmp_path):
    spec = _heavy_spec(tmp_path)
    primary = hw.primary_task(spec)
    frontiers = hw.frontier_tasks(spec)
    frontiers[0]["status"] = "rejected_approach"
    frontiers[1]["status"] = "retracted"
    assert hw.all_frontiers_rejected(spec) is True
    hw.advance_primary_if_ready(spec)
    assert primary["status"] == "pending"
    assert hw.compute_heavy_phase(spec) == "primary"


def test_frontier_accepted_outcome(tmp_path, monkeypatch):
    """Judge returns accepted_approach -> task gets that status."""
    spec = _heavy_spec(tmp_path)
    frontier = hw.frontier_tasks(spec)[0]

    def fake_judge(sp, task, ec, out, **kw):
        return 1, "check passed, approach is viable", [], "accepted_approach"

    monkeypatch.setattr(ssv, "judge_task", fake_judge)
    from spec_stop_validate import _validate_one_task

    _validate_one_task(spec, frontier, str(tmp_path))
    assert frontier["status"] == "accepted_approach"


def test_comparison_selects_best_frontier(tmp_path, monkeypatch):
    """All frontiers terminal, >=1 accepted -> comparison runs, winner selected."""
    spec = _heavy_spec(tmp_path)
    f1, f2 = hw.frontier_tasks(spec)
    f1["status"] = "accepted_approach"
    f1["exit"] = 0
    f1["output"] = "5 passed"
    f1["judge_reason"] = "viable"
    f2["status"] = "accepted_approach"
    f2["exit"] = 0
    f2["output"] = "3 passed"
    f2["judge_reason"] = "viable"
    primary = hw.primary_task(spec)

    import codex_judge

    def fake_ask(system, user, schema, schema_name=None):
        return {"selected_id": f1["id"], "selection_rationale": "More tests pass"}

    monkeypatch.setattr(codex_judge, "ask_structured", fake_ask)

    from spec import judge_frontier_comparison

    headlines = judge_frontier_comparison(spec)
    assert f1["comparison_winner"] is True
    assert f1["status"] == "accepted_approach"
    assert f2["status"] == "rejected_approach"
    assert primary["status"] == "superseded"
    assert hw.compute_heavy_phase(spec) == "adopted"
    assert any("selected as best frontier" in h for h in headlines)


def test_completion_via_adoption(tmp_path):
    """Accepted frontier with prior verdict=1, others resolved, primary superseded."""
    spec = _heavy_spec(tmp_path)
    f1, f2 = hw.frontier_tasks(spec)
    f1["status"] = "accepted_approach"
    f1["comparison_winner"] = True
    f1["judge_verdict"] = 1
    f2["status"] = "rejected_approach"
    primary = hw.primary_task(spec)
    primary["status"] = "superseded"
    ok, incomplete = all_tasks_validated(spec)
    assert ok, incomplete


def test_still_viable_does_not_trigger_comparison(tmp_path):
    """A non-terminal frontier blocks comparison."""
    spec = _heavy_spec(tmp_path)
    f1, f2 = hw.frontier_tasks(spec)
    f1["status"] = "accepted_approach"
    f1["exit"] = 0
    f1["output"] = "passed"
    # f2 is still pending (not terminal)
    assert hw.all_frontiers_terminal(spec) is False


def test_all_frontiers_rejected_still_works(tmp_path):
    """Regression: no accepted frontiers -> standard primary path."""
    spec = _heavy_spec(tmp_path)
    for t in hw.frontier_tasks(spec):
        t["status"] = "rejected_approach"
    hw.advance_primary_if_ready(spec)
    assert hw.compute_heavy_phase(spec) == "primary"
    assert hw.accepted_frontier(spec) is None


def test_primary_stays_blocked_with_accepted_frontier(tmp_path):
    """Primary stays blocked when a frontier is accepted but comparison hasn't run."""
    spec = _heavy_spec(tmp_path)
    frontiers = hw.frontier_tasks(spec)
    frontiers[0]["status"] = "accepted_approach"
    # Not all terminal yet (f2 still pending), no comparison_winner
    assert hw.compute_heavy_phase(spec) == "frontier"
    assert hw.primary_task(spec)["status"] == "blocked"


def test_heavy_workflow_brief_uses_glossary_and_declare_phase():
    brief = hw.heavy_workflow_brief(phase="declare")
    assert "Glossary: frontier = competing exploratory approach; primary = evidence-backed fallback." in brief
    assert "Declare phase:" in brief
    assert "Before edits:" not in brief
    assert "Stop runs frontier adjudication" in brief


def test_still_viable_outcome_is_not_stored_as_failed(tmp_path):
    """Regression: a still_viable frontier keeps its own status, not `failed`.

    Storing still_viable as `failed` (not in FRONTIER_RESOLVED) was the root of the
    livelock: the frontier never resolved and the primary stayed blocked forever.
    """
    spec = _heavy_spec(tmp_path)
    f1 = hw.frontier_tasks(spec)[0]
    ssv._apply_check_result(
        spec, f1, exit_code=0, output="viable", verdict=0, reason="viable but unselected",
        new_reqs=[], frontier_outcome="still_viable",
    )
    assert f1["status"] == "still_viable"
    assert "failed" not in {f1["status"]}
    assert hw.frontier_is_resolved(f1) is False  # still blocks until the arbiter fires


def test_frontier_stall_arbiter_respects_cap_then_unlocks_primary(tmp_path, monkeypatch):
    """The still_viable livelock gets a bounded escape: cap respected, then primary unlocks."""
    monkeypatch.setenv("UNIFABLE_FRONTIER_STALL_CAP", "2")
    spec = _heavy_spec(tmp_path)
    for t in hw.frontier_tasks(spec):
        t["status"] = "still_viable"

    # Stop #1: under the cap -> no unlock yet (one extra exploration round preserved).
    assert hw.resolve_frontier_stall(spec) == []
    assert hw.primary_task(spec)["status"] == "blocked"
    assert hw.compute_heavy_phase(spec) == "frontier"
    assert hw.all_tasks_validated_heavy(spec)[0] is False

    # Stop #2: hits the cap -> viable frontiers ruled out, primary fallback unlocks.
    headlines = hw.resolve_frontier_stall(spec)
    assert headlines  # emitted a transition message
    assert all(t["status"] == "rejected_approach" for t in hw.frontier_tasks(spec))
    assert hw.primary_task(spec)["status"] == "pending"
    assert hw.compute_heavy_phase(spec) == "primary"

    # Once the agent validates the now-unlocked primary, the spec completes.
    hw.primary_task(spec)["status"] = "validated"
    ok, incomplete = hw.all_tasks_validated_heavy(spec)
    assert ok is True and incomplete == []


def test_frontier_stall_arbiter_resets_on_progress(tmp_path, monkeypatch):
    """A pending/unexplored frontier means real work remains -> counter must reset."""
    monkeypatch.setenv("UNIFABLE_FRONTIER_STALL_CAP", "2")
    spec = _heavy_spec(tmp_path)
    frontiers = hw.frontier_tasks(spec)
    frontiers[0]["status"] = "still_viable"
    # frontiers[1] still pending -> not a settled stall.
    assert hw.resolve_frontier_stall(spec) == []
    assert spec.get("frontier_stall_blocks") == 0
    assert hw.primary_task(spec)["status"] == "blocked"


def test_frontier_stall_arbiter_ignores_accepted_frontier(tmp_path, monkeypatch):
    """An accepted frontier routes through adoption, not the stall arbiter."""
    monkeypatch.setenv("UNIFABLE_FRONTIER_STALL_CAP", "1")
    spec = _heavy_spec(tmp_path)
    frontiers = hw.frontier_tasks(spec)
    frontiers[0]["status"] = "accepted_approach"
    frontiers[1]["status"] = "still_viable"
    assert hw.resolve_frontier_stall(spec) == []
    assert frontiers[1]["status"] == "still_viable"  # not force-rejected
