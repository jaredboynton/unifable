"""Validate judge-tended completion self-healing (loop_release) from the spec's
point of view: a stalled spec polluted with spurious judge-added requirements
CONVERGES once the loop-release judge authorizes a permanent retraction, and
agent-authored requirements are never retracted. Complements the unit-level
test_loop_release.py with a spec-convergence (self-heal) view.

Origin: this session got trapped because the judge added paraphrase requirements
(T5-T9) that could never validate, and the only escape was the brute-force
stall-release. loop_release.py is the judge-tended healing that retracts those at
the source so the spec converges instead of relying on the backstop.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

from loop_release import LoopReleaseVerdict, apply_loop_release_verdict  # noqa: E402
from spec import all_tasks_validated  # noqa: E402


def _task(tid, status, added_by="agent"):
    return {"id": tid, "title": tid, "check": "true", "status": status, "added_by": added_by}


def test_permanent_lift_retracts_redundant_judge_tasks():
    """A permanent loop-release verdict retracts the named judge-added tasks;
    validated/agent tasks are left untouched."""
    spec = {"requires_tasks": True, "restated_goal": "g", "tasks": [
        _task("T1", "validated", "agent"),
        _task("T5", "failed", "judge"),
        _task("T6", "disputed", "judge"),
    ]}
    verdict = LoopReleaseVerdict(
        suicide_loop=True, lift="permanent",
        reason="spurious judge-added paraphrases of the validated requirement",
        lift_scope="", retract_task_ids=["T5", "T6"], provisional_stops=0,
    )
    headlines, _ = apply_loop_release_verdict(spec, {}, verdict)
    by = {t["id"]: t for t in spec["tasks"]}
    assert by["T5"]["status"] == "retracted"
    assert by["T6"]["status"] == "retracted"
    assert by["T1"]["status"] == "validated"  # validated/agent untouched
    assert headlines


def test_permanent_lift_never_retracts_agent_tasks():
    """The judge may only retract its OWN added requirements, never the agent's."""
    spec = {"requires_tasks": True, "tasks": [_task("T2", "failed", "agent")]}
    verdict = LoopReleaseVerdict(True, "permanent", "want it gone", "", ["T2"], 0)
    headlines, _ = apply_loop_release_verdict(spec, {}, verdict)
    assert spec["tasks"][0]["status"] == "failed"  # agent task never retracted
    assert headlines == []  # nothing retractable -> declined


def test_garden_heal_converges_a_stalled_spec():
    """End-to-end self-heal: a spec stuck only on a spurious judge task converges
    to fully-resolved after the loop-release retraction (the gate would open)."""
    spec = {"requires_tasks": True, "restated_goal": "g", "tasks": [
        _task("T1", "validated", "agent"),
        _task("T5", "failed", "judge"),
    ]}
    assert all_tasks_validated(spec)[0] is False  # stuck before heal
    verdict = LoopReleaseVerdict(True, "permanent", "redundant with validated T1", "", ["T5"], 0)
    apply_loop_release_verdict(spec, {}, verdict)
    assert all_tasks_validated(spec)[0] is True  # converged after heal -> breaker opens


def test_garden_declines_when_no_suicide_loop():
    """lift=none / suicide_loop=false changes nothing (no false retractions)."""
    spec = {"requires_tasks": True, "tasks": [
        _task("T1", "validated", "agent"),
        _task("T5", "failed", "judge"),
    ]}
    verdict = LoopReleaseVerdict(False, "none", "work legitimately remains", "", [], 0)
    headlines, _ = apply_loop_release_verdict(spec, {}, verdict)
    assert headlines == []
    assert {t["id"]: t["status"] for t in spec["tasks"]}["T5"] == "failed"  # unchanged


def test_deterministic_heal_retracts_brittle_version_pin():
    from spec import deterministic_heal_judge_requirements

    spec = {"requires_tasks": True, "restated_goal": "g", "tasks": [
        _task("T1", "validated", "agent"),
        {
            "id": "T9",
            "title": "Active plugin version is explicitly verified as 1.9.32",
            "check": "grep -q 1.9.32 .claude-plugin/plugin.json",
            "status": "failed",
            "added_by": "judge",
        },
    ]}
    headlines = deterministic_heal_judge_requirements(spec)
    assert spec["tasks"][1]["status"] == "retracted"
    assert headlines
    assert all_tasks_validated(spec)[0] is True


def test_judge_heal_revises_broken_judge_check(monkeypatch):
    from spec import judge_heal_own_requirements

    spec = {"requires_tasks": True, "restated_goal": "g", "tasks": [
        {
            "id": "T9",
            "title": "grep for pattern",
            "check": "rg -P 'foo' bar.py",
            "status": "failed",
            "added_by": "judge",
            "judge_reason": "needs portable grep",
        },
    ]}

    def fake_ask(_system, _user, _schema, schema_name=""):
        assert schema_name == "judge_heal"
        return {
            "adjust_requirements": [{
                "id": "T9",
                "action": "revise",
                "reason": "portable extended-regex grep",
                "check": "grep -E 'foo' bar.py",
            }],
        }

    import codex_judge
    monkeypatch.setattr(codex_judge, "ask_structured", fake_ask)
    with patch("spec.notify_spec_update"):
        headlines = judge_heal_own_requirements(spec)
    assert spec["tasks"][0]["check"] == "grep -E 'foo' bar.py"
    assert spec["tasks"][0]["status"] == "pending"
    assert headlines
