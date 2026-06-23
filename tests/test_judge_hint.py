"""Advisory judge hints: non-blocking nudges from the judge."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    _normalize_hint,
    all_tasks_validated,
    auto_validate_spec,
    judge_hint,
    load_spec,
    save_spec,
    spec_template,
)


def _task(tid, status):
    return {"id": tid, "title": tid, "check": "true", "status": status}


def _seed(tmp_path, status="pending"):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "do x"
    s["tasks"] = [_task("T1", status)]
    save_spec(str(tmp_path), "K", s)
    return str(tmp_path)


def test_normalize_hint_drops_empty_and_placeholders():
    assert _normalize_hint("") == ""
    assert _normalize_hint("   ") == ""
    assert _normalize_hint(None) == ""
    for ph in ("tbd", "N/A", "none", "no hint", "Nothing", "unsure"):
        assert _normalize_hint(ph) == "", ph


def test_auto_validate_pass_stores_merged_reason(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(
        spec_mod, "judge_tasks",
        lambda sp, items, *, transcript="": [(1, "ok — evidence accepted", [], "") for _ in items],
    )
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    t = spec["tasks"][0]
    assert t["status"] == "validated"
    assert t["judge_reason"] == "ok — evidence accepted"
    assert t.get("judge_hint", "") == ""


def test_failing_verdict_reason_includes_actionable_feedback(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (1, "fail"))
    monkeypatch.setattr(
        spec_mod, "judge_tasks",
        lambda sp, items, *, transcript="": [(
            0,
            "no real evidence — run the actual suite and capture output",
            [],
            "",
        ) for _ in items],
    )
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    t = spec["tasks"][0]
    assert t["status"] == "failed"
    assert "run the actual suite" in t["judge_reason"]
    ok, incomplete = all_tasks_validated(spec)
    assert not ok and incomplete == ["T1"]


def test_judge_hint_is_failopen_and_never_mutates_spec(monkeypatch):
    import codex_judge

    spec = {"restated_goal": "g", "tasks": [_task("T1", "failed")]}
    before = copy.deepcopy(spec)

    def boom(*a, **k):
        raise codex_judge.JudgeError("judge down")

    monkeypatch.setattr(codex_judge, "ask_structured", boom)
    assert judge_hint(spec, signal="looping", recent="x") == ""
    assert spec == before


def _run_stop(gate_stop, payload):
    captured = {"out": {}}
    gate_stop.read_stdin_json = lambda: payload
    gate_stop.emit_json = lambda d: captured.__setitem__("out", d)
    gate_stop.main()
    return captured["out"]


def test_stop_loop_appends_hint_at_threshold_without_lifting_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    import gate_stop

    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "ship the thing"
    spec["tasks"] = [_task("T1", "failed")]
    save_spec(str(tmp_path), "hintsess", spec)
    payload = {"session_id": "hintsess", "cwd": str(tmp_path)}
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (1, "fail"))
    monkeypatch.setattr(
        spec_mod, "judge_tasks",
        lambda sp, items, *, transcript="": [(0, "no", [], "") for _ in items],
    )
    monkeypatch.setattr(
        spec_mod, "judge_hint",
        lambda sp, *, signal, recent="": "fix the check before trying to finish",
    )

    outs = [_run_stop(gate_stop, payload) for _ in range(3)]
    assert all(o.get("decision") == "block" for o in outs)
    assert "fix the check" in outs[2].get("reason", "")
