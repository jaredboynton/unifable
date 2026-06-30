#!/usr/bin/env python3
"""Surface silent evidence-spec mutations to the model (gaps 1-7).

Each test exercises one notification path that was previously silent: citation
auto-sync (1), HEAVY phase/primary unblock (2), grade/profile reclassification
(3), validate-all adjust_requirements (4), scaffold mutation (5), breaker status
(6), and sub-agent citation attribution (7).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import breaker_state  # noqa: E402
import codex_judge  # noqa: E402
import gate_post_tool  # noqa: E402
import gate_prompt  # noqa: E402
import gate_stop  # noqa: E402
import spec as spec_mod  # noqa: E402
import spec_stop_validate as ssv  # noqa: E402
from citations import empty_activity, sync_citations_from_activity  # noqa: E402
from heavy_workflow import heavy_snapshot, heavy_transition_headline  # noqa: E402
from spec import auto_validate_spec, judge_all_tasks, load_spec, save_spec, spec_template  # noqa: E402

# --------------------------------------------------------------------------- #
# Gap 1 — citation auto-sync (added_sink tracks deterministic evidence mutations)
# --------------------------------------------------------------------------- #


def test_sync_added_sink_reports_appended_cites(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text("# x\n")
    spec = spec_template()
    activity = empty_activity()
    activity["read_paths"] = [str(f.resolve())]
    activity["fetched_urls"] = ["https://docs.example.com/guide"]
    sink: dict[str, list[str]] = {}
    assert sync_citations_from_activity(spec, activity, str(tmp_path), added_sink=sink) is True
    assert any("mod.py" in c for c in sink.get("repo_context", []))
    assert "https://docs.example.com/guide" in sink.get("prior_art", [])
    # idempotent: a second pass adds nothing, so the sink stays empty.
    sink2: dict[str, list[str]] = {}
    assert sync_citations_from_activity(spec, activity, str(tmp_path), added_sink=sink2) is False
    assert sink2 == {}


# --------------------------------------------------------------------------- #
# Gap 2 — HEAVY phase flip / primary unblock
# --------------------------------------------------------------------------- #


def _heavy_spec():
    return {
        "heavy_workflow": True,
        "tasks": [{"id": "T3", "approach_kind": "primary", "status": "pending"}],
    }


def test_heavy_transition_headline_primary_unblock():
    spec = _heavy_spec()
    headline = heavy_transition_headline(("frontier", "blocked"), ("primary", "pending"), spec)
    assert headline is not None
    assert "T3" in headline
    assert "unblocked" in headline
    assert "primary-path edits now allowed" in headline


def test_heavy_transition_headline_phase_flip_and_noop():
    spec = _heavy_spec()
    assert heavy_transition_headline(("declare", ""), ("frontier", ""), spec) == "HEAVY phase: declare -> frontier."
    assert heavy_transition_headline(("frontier", "pending"), ("frontier", "pending"), spec) is None


def test_heavy_snapshot_reads_phase_and_primary_status():
    spec = {"tasks": [{"id": "T3", "approach_kind": "primary", "status": "blocked"}]}
    assert heavy_snapshot(spec) == ("declare", "blocked")


# --------------------------------------------------------------------------- #
# Gap 3 — grade / evidence_profile reclassification (gate_prompt context)
# --------------------------------------------------------------------------- #


def _run_prompt(payload):
    captured = {"out": {}}
    gate_prompt.read_stdin_json = lambda: payload
    gate_prompt.emit_json = lambda d: captured.__setitem__("out", d)
    gate_prompt.main()
    return (captured["out"].get("hookSpecificOutput") or {}).get("additionalContext") or ""


def test_grade_change_surfaces_reason_and_shift(tmp_path, monkeypatch):
    import runtime_sync

    monkeypatch.setattr(runtime_sync, "sync_runtime", lambda *a, **k: False)
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_GRADE", raising=False)

    monkeypatch.setattr(
        gate_prompt,
        "judge_grade_classify",
        lambda *a, **k: {"mode": "normal", "risk_flags": [], "reason": "routine", "evidence_profile": "code"},
    )
    _run_prompt({"session_id": "sess", "prompt": "do a small thing", "cwd": str(tmp_path)})

    monkeypatch.setattr(
        gate_prompt,
        "judge_grade_classify",
        lambda *a, **k: {"mode": "deep", "risk_flags": [], "reason": "broad rearchitecture", "evidence_profile": "code"},
    )
    ctx = _run_prompt({"session_id": "sess", "prompt": "now deeply rearchitect the module across files", "cwd": str(tmp_path)})
    assert "HEAVY" in ctx
    assert "broad rearchitecture" in ctx


def test_no_reclassify_line_on_first_prompt(tmp_path, monkeypatch):
    import runtime_sync

    monkeypatch.setattr(runtime_sync, "sync_runtime", lambda *a, **k: False)
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_GRADE", raising=False)
    monkeypatch.setattr(
        gate_prompt,
        "judge_grade_classify",
        lambda *a, **k: {"mode": "normal", "risk_flags": [], "reason": "routine", "evidence_profile": "code"},
    )
    ctx = _run_prompt({"session_id": "fresh", "prompt": "first task here", "cwd": str(tmp_path)})
    assert "Reclassified:" not in ctx


# --------------------------------------------------------------------------- #
# Gap 4 — validate-all adjust_requirements headlines reach val_msgs
# --------------------------------------------------------------------------- #


def test_auto_validate_merges_adjust_headlines_into_msgs(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [{"id": "T1", "title": "t", "check": "true", "status": "pending"}]
    save_spec(str(tmp_path), "K", s)

    def fake_judge_tasks(sp, items, *, transcript="", **kw):
        sp.setdefault("_stop_adjust_headlines", []).append("Judge retracted T2: redundant")
        return [(1, "ok", [], "") for _ in items]

    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks", fake_judge_tasks)
    _spec, msgs = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert any("Judge retracted T2: redundant" in m for m in msgs)


def test_judge_all_tasks_stashes_adjust_headlines(tmp_path, monkeypatch):
    s = spec_template()
    s["restated_goal"] = "g"
    s["tasks"] = [
        {"id": "T1", "title": "t", "check": "true", "status": "pending"},
        {"id": "T2", "title": "j", "check": "true", "status": "pending", "added_by": "judge"},
    ]

    def fake_ask_structured(system, user, schema, *, schema_name="", timeout=None, **kw):
        return {
            "task_verdicts": [
                {
                    "id": "T1",
                    "verdict": 1,
                    "reason": "ok",
                    "adjust_requirements": [{"id": "T2", "action": "retract", "reason": "redundant"}],
                }
            ]
        }

    monkeypatch.setattr(codex_judge, "ask_structured", fake_ask_structured)
    items = [{"task": s["tasks"][0], "kind": "validate", "exit_code": 0, "output": "ok"}]
    judge_all_tasks(s, items)
    assert any("T2" in h for h in s.get("_stop_adjust_headlines", []))
    assert s["tasks"][1]["status"] == "retracted"


# --------------------------------------------------------------------------- #
# Gap 5 — scaffold mutations reported
# --------------------------------------------------------------------------- #


def test_scaffold_reports_cleared_heavy_and_profile_change(tmp_path):
    s = spec_template()
    s["heavy_workflow"] = True
    s["restated_goal"] = "g"
    s["evidence_profile"] = "code"
    s["tasks"] = []
    save_spec(str(tmp_path), "K", s)
    path, changes, _created = spec_mod.ensure_spec_scaffold(
        str(tmp_path), "K", "prompt", heavy=False, evidence_profile="operational"
    )
    assert path
    assert any("cleared stale heavy_workflow" in c for c in changes)
    assert any("operational" in c for c in changes)


def test_scaffold_fresh_create_reports_no_changes(tmp_path):
    path, changes, created = spec_mod.ensure_spec_scaffold(str(tmp_path), "NEW", "prompt", heavy=False, evidence_profile="code")
    assert path
    assert changes == []
    assert created is True


# --------------------------------------------------------------------------- #
# Gap 6 — breaker status line
# --------------------------------------------------------------------------- #


def test_breaker_status_context_armed_and_open(monkeypatch):
    # PostToolUse no longer narrates standing breaker state (the PreToolUse
    # one-shot notify is the single source). Both armed and disarmed/provisional
    # states must yield "" so no breaker line reaches PostToolUse additionalContext.
    monkeypatch.setattr(
        breaker_state,
        "load_breaker",
        lambda inp: {"breaker_armed": True, "breaker_claim": "the build passes"},
    )
    assert gate_post_tool._breaker_status_context({}) == ""

    monkeypatch.setattr(
        breaker_state,
        "load_breaker",
        lambda inp: {"breaker_armed": False, "breaker_provisional": False},
    )
    assert gate_post_tool._breaker_status_context({}) == ""


# --------------------------------------------------------------------------- #
# Gap 7 — sub-agent / transcript citation attribution
# --------------------------------------------------------------------------- #


def test_subagent_attribution_credits_transcript_only_cites(tmp_path):
    f = tmp_path / "sub.py"
    f.write_text("# x\n")
    transcript_activity = empty_activity()
    transcript_activity["read_paths"] = [str(f.resolve())]
    transcript_activity["fetched_urls"] = ["https://sub.example/x"]
    spec = spec_template()
    sink: dict[str, list[str]] = {}
    sync_citations_from_activity(spec, transcript_activity, str(tmp_path), added_sink=sink)

    ledger_only = empty_activity()  # the main model did nothing directly
    note = gate_stop._subagent_attribution(sink, ledger_only, transcript_activity, str(tmp_path))
    assert "credited sub-agent/transcript activity" in note
    assert "2 citation(s)" in note

    # When the ledger already supports the cites, nothing is credited to sub-agents.
    note2 = gate_stop._subagent_attribution(sink, transcript_activity, transcript_activity, str(tmp_path))
    assert note2 == ""
