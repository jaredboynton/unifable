"""Append-only spec lifecycle: the agent adds requirements + evidence, only the
judge removes (via dispute -> retracted), and the judge may add requirements.

Covers the corrected model:
- `requires_tasks` (set by the auto-creation hook) makes an empty spec NON-completable.
- a task is resolved (does not block completion) only when validated or retracted.
- `dispute` records an impossibility claim (status disputed); the judge adjudicates
  on validate-task: accept -> retracted, reject -> failed with feedback.
- the judge may append new requirements while judging a task (append-only).

The network judge is stubbed by reassigning spec.judge_task / spec.judge_dispute,
matching the gate_stop._judge_goal_condition pattern in the suite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    all_tasks_validated,
    load_spec,
    save_spec,
    spec_template,
    _cmd_dispute,
    _cmd_validate_task,
)


def _task(tid, status):
    return {"id": tid, "title": tid, "check": "true", "status": status}


# --- all_tasks_validated semantics -----------------------------------------

def test_requires_tasks_empty_spec_blocks_completion():
    s = spec_template(); s["requires_tasks"] = True; s["tasks"] = []
    ok, incomplete = all_tasks_validated(s)
    assert not ok and incomplete


def test_legacy_empty_spec_without_requires_tasks_still_passes():
    s = spec_template(); s["tasks"] = []
    assert all_tasks_validated(s) == (True, [])


def test_retracted_counts_as_resolved():
    s = {"requires_tasks": True, "tasks": [_task("T1", "validated"), _task("T2", "retracted")]}
    assert all_tasks_validated(s) == (True, [])


def test_disputed_does_not_resolve():
    s = {"requires_tasks": True, "tasks": [_task("T1", "validated"), _task("T2", "disputed")]}
    ok, incomplete = all_tasks_validated(s)
    assert not ok and incomplete == ["T2"]


# --- dispute records the claim, does not remove ----------------------------

def _seed(tmp_path, status="failed"):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "do x"
    s["tasks"] = [_task("T1", status)]
    save_spec(str(tmp_path), "K", s)
    return SimpleNamespace(root=str(tmp_path), task_id="K", task="T1")


def test_dispute_sets_status_and_stores_evidence(tmp_path):
    args = _seed(tmp_path)
    args.evidence = "the upstream API has no such endpoint; 404 on every variant"
    rc = _cmd_dispute(args)
    assert rc == 0
    t = load_spec(str(tmp_path), "K")["tasks"][0]
    assert t["status"] == "disputed"
    assert "404" in t["dispute_evidence"]


def test_cannot_dispute_a_validated_task(tmp_path):
    args = _seed(tmp_path, status="validated")
    args.evidence = "nope"
    assert _cmd_dispute(args) == 1
    assert load_spec(str(tmp_path), "K")["tasks"][0]["status"] == "validated"


# --- judge adjudicates a dispute on validate-task --------------------------

def test_validate_task_accepts_dispute_and_retracts(tmp_path, monkeypatch):
    args = _seed(tmp_path)
    args.evidence = "proven impossible"
    _cmd_dispute(args)
    monkeypatch.setattr(spec_mod, "judge_dispute", lambda s, t, e: (1, "genuinely impossible"))
    rc = _cmd_validate_task(args)
    assert rc == 0
    assert load_spec(str(tmp_path), "K")["tasks"][0]["status"] == "retracted"


def test_validate_task_rejects_dispute_and_fails(tmp_path, monkeypatch):
    args = _seed(tmp_path)
    args.evidence = "i just don't wanna"
    _cmd_dispute(args)
    monkeypatch.setattr(spec_mod, "judge_dispute", lambda s, t, e: (0, "do better"))
    rc = _cmd_validate_task(args)
    assert rc == 2
    assert load_spec(str(tmp_path), "K")["tasks"][0]["status"] == "failed"


# --- judge adds requirements while judging ---------------------------------

def test_validate_task_appends_judge_requirements(tmp_path, monkeypatch):
    s = spec_template(); s["requires_tasks"] = True; s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "delivered")]
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(
        spec_mod, "judge_task",
        lambda sp, t, ec, out: (1, "ok", [{"title": "also handle errors", "check": "true"}]),
    )
    args = SimpleNamespace(root=str(tmp_path), task_id="K", task="T1")
    rc = _cmd_validate_task(args)
    assert rc == 0
    tasks = load_spec(str(tmp_path), "K")["tasks"]
    assert [t["id"] for t in tasks] == ["T1", "T2"]
    assert tasks[0]["status"] == "validated"
    assert tasks[1]["status"] == "pending" and tasks[1]["added_by"] == "judge"


# --- cite appends evidence (append-only; the agent's only way to add it) ----

def test_cite_appends_repo_context_and_prior_art(tmp_path):
    from spec import _cmd_cite
    s = spec_template(); s["requires_tasks"] = True; s["tasks"] = [_task("T1", "pending")]
    s["repo_context"] = []; s["prior_art"] = []  # mirror the hook/create scaffold (no placeholder)
    save_spec(str(tmp_path), "K", s)
    args = SimpleNamespace(
        root=str(tmp_path), task_id="K",
        repo_context=["a.py:1::why a", "b.py:2::why b"],
        prior_art=["https://x::backs it"],
    )
    assert _cmd_cite(args) == 0
    out = load_spec(str(tmp_path), "K")
    assert [c["cite"] for c in out["repo_context"]] == ["a.py:1", "b.py:2"]
    assert out["prior_art"][0]["cite"] == "https://x"
    # appends, never replaces
    _cmd_cite(SimpleNamespace(root=str(tmp_path), task_id="K", repo_context=["c.py:3::w"], prior_art=[]))
    assert len(load_spec(str(tmp_path), "K")["repo_context"]) == 3


# --- the auto-creation hook ------------------------------------------------

def test_scaffold_hook_creates_requires_tasks_spec(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    import gate_prompt
    path = gate_prompt._ensure_spec_scaffold(str(tmp_path), "K", "Refactor the auth module please")
    assert path and Path(path).exists()
    s = load_spec(str(tmp_path), "K")
    assert s["requires_tasks"] is True
    assert s["tasks"] == []
    assert "auth module" in s["restated_goal"]
    # empty scaffold is NOT completable
    assert all_tasks_validated(s)[0] is False
    # idempotent: a second call does not overwrite an existing spec
    s2 = load_spec(str(tmp_path), "K"); s2["tasks"] = [_task("T1", "validated")]
    save_spec(str(tmp_path), "K", s2)
    gate_prompt._ensure_spec_scaffold(str(tmp_path), "K", "different prompt")
    assert load_spec(str(tmp_path), "K")["tasks"] == [_task("T1", "validated")]


def test_end_to_end_append_only_flow(tmp_path, monkeypatch):
    """scaffold -> add-task -> cite -> deliver -> validate-task(pass) -> complete."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    import gate_prompt
    from spec import _cmd_add_task, _cmd_cite, _cmd_deliver, validate_spec
    gate_prompt._ensure_spec_scaffold(str(tmp_path), "K", "do the thing")
    _cmd_add_task(SimpleNamespace(root=str(tmp_path), task_id="K", title="thing works", check="true"))
    _cmd_cite(SimpleNamespace(root=str(tmp_path), task_id="K",
                              repo_context=["a.py:1::why"], prior_art=["https://x::why"]))
    # spec now validates at STANDARD with evidence
    ok, reasons = validate_spec(load_spec(str(tmp_path), "K"), "STANDARD", require_evidence=True)
    assert ok, reasons
    _cmd_deliver(SimpleNamespace(root=str(tmp_path), task_id="K", task="T1"))
    monkeypatch.setattr(spec_mod, "judge_task", lambda sp, t, ec, out: (1, "ok", []))
    assert _cmd_validate_task(SimpleNamespace(root=str(tmp_path), task_id="K", task="T1")) == 0
    assert all_tasks_validated(load_spec(str(tmp_path), "K")) == (True, [])
