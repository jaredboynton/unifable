"""Supersedes bundle, agent revise, and fragmentation detection."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import spec as spec_mod  # noqa: E402
from loop_release import stall_signature  # noqa: E402
from spec import (  # noqa: E402
    _apply_adjustments,
    _apply_supersedes_bundle,
    all_tasks_validated,
    auto_validate_spec,
    detect_requirement_fragmentation,
    load_spec,
    save_spec,
    spec_template,
)


def _task(tid, status, *, added_by="agent", check="true", title=None):
    t = {
        "id": tid, "title": title or tid, "check": check, "status": status,
        "added_by": added_by, "attempts": 0,
    }
    if status == "failed":
        t["exit"] = 1
        t["output"] = "fail"
    return t


def test_supersedes_marks_agent_tasks_non_blocking():
    spec = {"requires_tasks": True, "tasks": [
        _task("T1", "failed"),
        _task("T2", "failed"),
    ]}
    _apply_supersedes_bundle(spec, "T10", ["T1", "T2"])
    assert spec["tasks"][0]["status"] == "superseded"
    assert spec["tasks"][0]["superseded_by"] == "T10"
    assert spec["tasks"][1]["status"] == "superseded"
    ok, incomplete = all_tasks_validated(spec)
    assert ok is True
    assert incomplete == []


def test_supersedes_retracts_judge_tasks():
    spec = {"requires_tasks": True, "tasks": [
        _task("T5", "failed", added_by="judge"),
    ]}
    _apply_supersedes_bundle(spec, "T10", ["T5"])
    assert spec["tasks"][0]["status"] == "retracted"


def test_new_requirement_with_supersedes_drops_breaker_count(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task(f"T{i}", "failed") for i in range(1, 4)]
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(
        spec_mod, "judge_tasks",
            lambda sp, items: [(
                0, "still failing",
                [{"title": "replacement", "check": "test -f x", "supersedes": ["T1", "T2", "T3"]}],
                "",
            ) for _ in items],
    )
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    by = {t["id"]: t for t in spec["tasks"]}
    assert by["T1"]["status"] == "superseded"
    assert by["T2"]["status"] == "superseded"
    assert by["T3"]["status"] == "superseded"
    pending = [t for t in spec["tasks"] if t.get("status") == "pending"]
    assert len(pending) == 1
    assert all_tasks_validated(spec)[0] is False  # one pending replacement left


def test_agent_revise_skips_verdict_this_stop(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["tasks"] = [_task("T1", "failed", check="bad prose check")]
    save_spec(str(tmp_path), "K", s)

    def fake_judge(sp, t, ec, out):
        _apply_adjustments(sp, {
            "adjust_requirements": [{
                "id": "T1", "action": "revise",
                "reason": "check was non-executable",
                "check": "test -f scratchpad/SPEC.md",
            }],
        })
        return 0, "still failing old output", [], ""

    monkeypatch.setattr(spec_mod, "judge_task", fake_judge)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (1, "fail"))
    spec, msgs = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    t = spec["tasks"][0]
    assert t["status"] == "pending"
    assert t["check"] == "test -f scratchpad/SPEC.md"
    assert any("revised" in m for m in msgs)


def test_detect_fragmentation_many_failed_plus_judge_pending():
    spec = {"tasks": [_task(f"T{i}", "failed") for i in range(1, 7)]}
    spec["tasks"] += [
        _task("T10", "pending", added_by="judge", title="written spec exists"),
    ]
    frag = detect_requirement_fragmentation(spec)
    assert frag is not None
    assert frag["failed_count"] >= 3
    assert "T10" in frag["pending_judge_ids"]


def test_stall_signature_true_on_fragmentation():
    spec = {"tasks": [_task(f"T{i}", "failed") for i in range(1, 8)]}
    spec["tasks"] += [_task("T10", "pending", added_by="judge")]
    led = {}
    assert stall_signature(led, ["T1", "T2", "T10"], spec=spec) is True
