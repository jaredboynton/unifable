#!/usr/bin/env python3
"""HEAVY adoption finalization: deterministic winner selection."""

from __future__ import annotations

import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import heavy_workflow as hw  # noqa: E402
from heavy_workflow import (  # noqa: E402
    accepted_frontier,
    all_tasks_validated_heavy,
    compute_heavy_phase,
    finalize_heavy_adoption,
)
from spec import (  # noqa: E402
    append_frontier_task,
    save_spec,
    set_primary_task,
    spec_template,
)


def _heavy_spec(tmp_path):
    spec = spec_template()
    spec["heavy_workflow"] = True
    spec["requires_tasks"] = True
    spec["restated_goal"] = "Ship cache-decoupled runtime"
    spec["goal_seeded"] = False
    spec["acceptance_criteria"] = []
    spec["repo_context"] = [{"cite": "src/a.py:1", "why": "read this session"}]
    spec["prior_art"] = [{"cite": "https://example.com/doc", "why": "fetched this session"}]
    spec["tasks"] = []
    append_frontier_task(spec, "Cache-decoupled hooks", "pytest tests/test_cache.py -q")
    append_frontier_task(spec, "Inline cache path", "pytest tests/test_inline.py -q")
    set_primary_task(spec, "Legacy cache", "pytest tests/test_legacy.py -q")
    save_spec(str(tmp_path), "K", spec)
    return spec


def test_finalize_heavy_adoption_selects_winner_and_supersedes_primary():
    spec = _heavy_spec(Path("/tmp/unused"))
    frontiers = hw.frontier_tasks(spec)
    frontiers[0]["status"] = "accepted_approach"
    frontiers[0]["exit"] = 0
    frontiers[0]["output"] = "ok"
    frontiers[1]["status"] = "rejected_approach"
    primary = hw.primary_task(spec)
    assert primary is not None
    assert primary["status"] == "blocked"

    headlines = finalize_heavy_adoption(spec)
    assert headlines
    assert accepted_frontier(spec) is frontiers[0]
    assert frontiers[0].get("comparison_winner") is True
    assert primary["status"] == "superseded"
    assert compute_heavy_phase(spec) == "adopted"


def test_finalize_heavy_adoption_idempotent():
    spec = _heavy_spec(Path("/tmp/unused"))
    for t in hw.frontier_tasks(spec):
        t["status"] = "rejected_approach"
    hw.frontier_tasks(spec)[0]["status"] = "accepted_approach"
    hw.frontier_tasks(spec)[0]["exit"] = 0
    finalize_heavy_adoption(spec)
    assert finalize_heavy_adoption(spec) == []


def test_all_tasks_validated_heavy_passes_after_adoption(tmp_path):
    spec = _heavy_spec(tmp_path)
    frontiers = hw.frontier_tasks(spec)
    frontiers[0]["status"] = "accepted_approach"
    frontiers[0]["exit"] = 0
    frontiers[1]["status"] = "rejected_approach"
    finalize_heavy_adoption(spec)
    ok, incomplete = all_tasks_validated_heavy(spec)
    assert ok, incomplete


def test_validated_comparison_winner_opens_breaker():
    spec = _heavy_spec(Path("/tmp/unused"))
    f1, f2 = hw.frontier_tasks(spec)
    f1["status"] = "validated"
    f1["comparison_winner"] = True
    f1["judge_verdict"] = 1
    f2["status"] = "rejected_approach"
    primary = hw.primary_task(spec)
    assert primary is not None
    primary["status"] = "superseded"
    ok, incomplete = all_tasks_validated_heavy(spec)
    assert ok, incomplete


def test_adopted_frontier_not_pending():
    from spec import _task_is_pending

    task = {
        "id": "T4",
        "approach_kind": "frontier",
        "status": "validated",
        "comparison_winner": True,
    }
    assert _task_is_pending(task) is False


def test_finalize_picks_lower_exit_among_accepted():
    spec = _heavy_spec(Path("/tmp/unused"))
    f0, f1 = hw.frontier_tasks(spec)
    f0["status"] = "accepted_approach"
    f0["exit"] = 1
    f1["status"] = "accepted_approach"
    f1["exit"] = 0
    finalize_heavy_adoption(spec)
    winner = accepted_frontier(spec)
    assert winner is f1
