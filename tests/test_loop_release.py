#!/usr/bin/env python3
"""Completion loop lift: signature detection, judge verdicts, gate_stop integration."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import loop_release as lr  # noqa: E402
from ledger import DEFAULT_LEDGER, load_ledger, save_ledger  # noqa: E402
from spec import load_spec, save_spec, spec_template  # noqa: E402
from verify_state import COMPLETION_MAX_STALLED_BLOCKS, note_completion_block  # noqa: E402


def _task(tid, status, *, added_by=None, check="true"):
    t = {"id": tid, "title": tid, "check": check, "status": status}
    if added_by:
        t["added_by"] = added_by
    return t


def test_stall_signature_at_stop_block_threshold():
    led = {"completion_stop_blocks": 3}
    assert lr.stall_signature(led, ["T1"], pending_block=True) is True
    assert lr.stall_signature(led, ["T1"], pending_block=False) is False


def test_stall_signature_at_stall_blocks():
    led = {"completion_stall_blocks": lr.LOOP_STALL_SIGNATURE_BLOCKS}
    assert lr.stall_signature(led, ["T1", "T2"]) is True


def test_update_loop_signature_tracks_same_set_streak():
    led = {}
    lr.update_loop_signature(led, ["T2", "T1"])
    assert led["loop_same_set_streak"] == 1
    assert led["loop_episode_id"] == "T1,T2"
    lr.update_loop_signature(led, ["T1", "T2"])
    assert led["loop_same_set_streak"] == 2


def test_should_invoke_loop_judge_debounces_same_episode():
    led = {
        "completion_stall_blocks": 3,
        "loop_episode_id": "T1",
        "loop_judge_episode_id": "T1",
        "loop_judge_last_at": lr.time.monotonic(),
    }
    assert lr.should_invoke_loop_judge(led, ["T1"], pending_block=True) is False


def test_apply_provisional_verdict_sets_budget():
    spec = spec_template()
    spec["tasks"] = [_task("T1", "failed")]
    led = {"loop_episode_id": "T1"}
    verdict = lr.LoopReleaseVerdict(
        True, "provisional", "stuck on bad check", "fix the check command", [], 2
    )
    headlines, msg = lr.apply_loop_release_verdict(spec, led, verdict)
    assert led["loop_lift_stops_remaining"] == 2
    assert led["loop_lift_kind"] == "provisional"
    assert headlines
    assert "provisional" in msg


def test_consume_provisional_stop_lift_decrements():
    led = {"loop_lift_kind": "provisional", "loop_lift_stops_remaining": 2}
    assert lr.consume_provisional_stop_lift(led) is True
    assert led["loop_lift_stops_remaining"] == 1
    assert lr.consume_provisional_stop_lift(led) is True
    assert led["loop_lift_stops_remaining"] == 0
    assert led["loop_lift_kind"] == ""


def test_apply_permanent_retracts_judge_added_only():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["tasks"] = [
        _task("T1", "pending"),
        _task("T2", "failed", added_by="judge"),
    ]
    led = {"loop_episode_id": "T1,T2"}
    verdict = lr.LoopReleaseVerdict(
        True, "permanent", "T2 is spurious runaway", "", ["T1", "T2"], 0
    )
    with patch("spec.notify_spec_update"):
        headlines, msg = lr.apply_loop_release_verdict(spec, led, verdict)
    assert spec["tasks"][0]["status"] == "pending"
    assert spec["tasks"][1]["status"] == "retracted"
    assert "T2" in led["loop_lift_retracted"]
    assert headlines or msg


def test_permanent_redundancy_retract_still_judge_added_only():
    spec = spec_template()
    spec["tasks"] = [
        _task("T1", "validated", added_by="agent"),
        _task("T2", "failed", added_by="judge"),
    ]
    led = {"loop_episode_id": "T1,T2"}
    verdict = lr.LoopReleaseVerdict(
        True, "permanent", "T2 duplicates validated T1", "", ["T1", "T2"], 0
    )
    with patch("spec.notify_spec_update"):
        lr.apply_loop_release_verdict(spec, led, verdict)
    assert spec["tasks"][0]["status"] == "validated"
    assert spec["tasks"][1]["status"] == "retracted"
    assert led["loop_lift_retracted"] == ["T2"]


def test_apply_declined_verdict_no_state_change():
    spec = spec_template()
    spec["tasks"] = [_task("T1", "failed")]
    led = {"loop_episode_id": "T1", "loop_lift_stops_remaining": 0}
    verdict = lr.LoopReleaseVerdict(False, "none", "legit work remains", "", [], 0)
    headlines, msg = lr.apply_loop_release_verdict(spec, led, verdict)
    assert not headlines and not msg
    assert led.get("loop_lift_kind", "") == ""


def test_judge_error_fail_open():
    spec = spec_template()
    led = {}
    import codex_judge

    with patch("codex_judge.ask_structured", side_effect=codex_judge.JudgeError("down")):
        verdict = lr.judge_completion_loop_release(spec, led, signal="stuck")
    assert verdict.lift == "none"


def test_stall_cap_still_releases_without_loop_judge():
    led = {}
    released = False
    for n in range(5, 5 + COMPLETION_MAX_STALLED_BLOCKS + 1):
        released = note_completion_block(led, n)
    assert released is True


def test_loop_fields_in_default_ledger():
    for key in (
        "completion_prev_incomplete_set",
        "loop_episode_id",
        "loop_lift_stops_remaining",
        "loop_events",
    ):
        assert key in DEFAULT_LEDGER


def test_stall_counters_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    inp = {"session_id": "loop-persist", "cwd": str(tmp_path)}
    led = load_ledger(inp)
    lr.update_loop_signature(led, ["T1"])
    led["loop_lift_kind"] = "provisional"
    led["loop_lift_stops_remaining"] = 2
    save_ledger(inp, led)
    reloaded = load_ledger(inp)
    assert reloaded["loop_lift_stops_remaining"] == 2


def _run_stop(gate_stop, payload):
    captured = {"out": {}}
    gate_stop.read_stdin_json = lambda: payload
    gate_stop.emit_json = lambda d: captured.__setitem__("out", d)
    gate_stop.main()
    return captured["out"]


def test_gate_stop_provisional_lift_allows_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop
    import spec as spec_mod

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "failed")]
    save_spec(str(tmp_path), "loopsess", s)

    led = load_ledger({"session_id": "loopsess", "cwd": str(tmp_path)})
    led["loop_lift_kind"] = "provisional"
    led["loop_lift_stops_remaining"] = 1
    led["loop_lift_reason"] = "test lift"
    led["loop_lift_scope"] = "fix the check"
    save_ledger({"session_id": "loopsess", "cwd": str(tmp_path)}, led)

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items: [(0, "no", [], "", "") for _ in items])

    out = _run_stop(gate_stop, {"session_id": "loopsess", "cwd": str(tmp_path)})
    assert out.get("decision") != "block"
    assert "provisional Stop lift" in (out.get("systemMessage") or "")


def test_gate_stop_loop_judge_provisional_then_allow(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop
    import spec as spec_mod

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "failed")]
    save_spec(str(tmp_path), "loopsess2", s)

    led = load_ledger({"session_id": "loopsess2", "cwd": str(tmp_path)})
    led["completion_stop_blocks"] = 3
    led["completion_stall_blocks"] = 3
    lr.update_loop_signature(led, ["T1"])
    save_ledger({"session_id": "loopsess2", "cwd": str(tmp_path)}, led)

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items: [(0, "no", [], "", "") for _ in items])

    verdict = lr.LoopReleaseVerdict(
        True, "provisional", "loop detected", "rewrite check", [], 1
    )
    with patch.object(lr, "judge_completion_loop_release", return_value=verdict):
        out = _run_stop(gate_stop, {"session_id": "loopsess2", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    assert "breaker CLOSED" in (out.get("reason") or "")

    reloaded = load_ledger({"session_id": "loopsess2", "cwd": str(tmp_path)})
    assert reloaded["loop_lift_stops_remaining"] == 1

    out2 = _run_stop(gate_stop, {"session_id": "loopsess2", "cwd": str(tmp_path)})
    assert out2.get("decision") != "block"
    assert "provisional Stop lift" in (out2.get("systemMessage") or "")


def test_gate_stop_loop_judge_decline_surfaced(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop
    import spec as spec_mod

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "failed")]
    save_spec(str(tmp_path), "loopdecl", s)

    led = load_ledger({"session_id": "loopdecl", "cwd": str(tmp_path)})
    led["completion_stop_blocks"] = 3
    led["completion_stall_blocks"] = 3
    lr.update_loop_signature(led, ["T1"])
    save_ledger({"session_id": "loopdecl", "cwd": str(tmp_path)}, led)

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items: [(0, "no", [], "", "") for _ in items])

    verdict = lr.LoopReleaseVerdict(False, "none", "legit work remains", "", [], 0)
    with patch.object(lr, "judge_completion_loop_release", return_value=verdict):
        out = _run_stop(gate_stop, {"session_id": "loopdecl", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    blob = ((out.get("reason") or "") + " " + (
        (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    )).lower()
    assert "no suicide loop" in blob or "legitimate work" in blob


def test_gate_stop_permanent_retract_opens_breaker(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop
    import spec as spec_mod

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "failed", added_by="judge")]
    save_spec(str(tmp_path), "loopsess3", s)

    led = load_ledger({"session_id": "loopsess3", "cwd": str(tmp_path)})
    led["completion_stall_blocks"] = 3
    lr.update_loop_signature(led, ["T1"])
    save_ledger({"session_id": "loopsess3", "cwd": str(tmp_path)}, led)

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items: [(1, "ok", [], "", "") for _ in items])
    monkeypatch.setattr(
        spec_mod, "auto_validate_spec", lambda spec, cwd, **kw: (spec, [])
    )

    verdict = lr.LoopReleaseVerdict(
        True, "permanent", "spurious judge req", "", ["T1"], 0
    )
    with patch.object(lr, "judge_completion_loop_release", return_value=verdict):
        out = _run_stop(gate_stop, {"session_id": "loopsess3", "cwd": str(tmp_path)})
    assert out.get("decision") != "block"
    assert load_spec(str(tmp_path), "loopsess3")["tasks"][0]["status"] == "retracted"
