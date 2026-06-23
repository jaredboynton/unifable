#!/usr/bin/env python3
"""Tests for HEAVY frontier-first approach workflow."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import heavy_workflow as hw  # noqa: E402
import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    _cmd_add_frontier,
    _cmd_set_primary,
    _cmd_restate,
    all_tasks_validated,
    append_frontier_task,
    judge_discover_frontiers,
    load_spec,
    save_spec,
    set_primary_task,
    spec_template,
    validate_spec,
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
    _cmd_add_frontier(SimpleNamespace(
        root=str(tmp_path), task_id="K", title="JWT with rotation", check="pytest tests/test_jwt.py",
    ))
    _cmd_add_frontier(SimpleNamespace(
        root=str(tmp_path), task_id="K", title="Session cookies hardened", check="pytest tests/test_sess.py",
    ))
    rc = _cmd_set_primary(SimpleNamespace(
        root=str(tmp_path), task_id="K", title="HMAC bearer tokens", check="pytest tests/test_hmac.py",
    ))
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

    def fake_judge(sp, task, ec, out):
        return 0, "broken boundary: latency regression", [], "rejected_approach"

    monkeypatch.setattr(spec_mod, "judge_task", fake_judge)
    from spec import _validate_one_task
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
