"""Obsolescence dispute: a requirement obsoleted by a legitimate pivot (the
behavior/route/file it constrains was removed, proven by a failable absence
check) must be retractable through the adjudicated dispute path -- not only on
impossibility.

Before this change the dispute rubric accepted impossibility ONLY, so an agent
whose work pivoted deadlocked: dispute rejected, and no agent-side retraction
path existed for an obsolete (but not strictly impossible) requirement. These
tests pin the rubric wiring in BOTH adjudication paths -- the batch validate-all
system (multi-task Stop) and the single judge_dispute path -- and prove an
accepted obsolescence dispute retracts the task.
"""

from __future__ import annotations

import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    _apply_dispute,
    _validate_all_system,
    judge_dispute,
    spec_template,
)


def _disputed_task():
    return {
        "id": "T1",
        "title": "Codex callback state consumed by listener flow without breaking fallback",
        "check": "exercise the listener callback path",
        "status": "disputed",
        "added_by": "agent",
        "attempts": 0,
        "dispute_evidence": (
            "grep -rn 'auth/codex/login|local listener|LISTENER' orchestrator/src "
            "-> 0 matches; the listener flow this requirement constrains was removed."
        ),
    }


def _assert_obsolescence(text):
    low = text.lower()
    assert "obsolesc" in low or "obsolete" in low, "rubric missing the obsolescence ground"
    assert "absent" in low or "removed" in low, "rubric missing the absence-proof demand"


def test_batch_validate_system_instructs_obsolescence_acceptance():
    # The batch validate-all path (the multi-task Stop that judged T1/T2/T3
    # together) must carry the obsolescence rule, or a pivoted dispute deadlocks.
    _assert_obsolescence(_validate_all_system(""))


def test_single_dispute_prompt_carries_obsolescence_rule(monkeypatch):
    # The single judge_dispute path must send the obsolescence rule to the judge.
    captured = {}

    def fake_ask_structured(system, user, schema, *, schema_name=None):
        captured["system"] = system
        return {"verdict": 1, "reason": "subject removed from repo"}

    monkeypatch.setattr("judge_transport.ask_structured", fake_ask_structured)
    s = {"restated_goal": "g", "tasks": [_disputed_task()]}
    verdict, _ = judge_dispute(s, s["tasks"][0], s["tasks"][0]["dispute_evidence"])
    assert verdict == 1
    _assert_obsolescence(captured["system"])


def test_accepted_obsolescence_dispute_retracts_task(monkeypatch):
    # When the judge accepts the obsolescence claim, the task is retracted
    # (a resolved status), unblocking completion -- the deadlock is broken.
    monkeypatch.setattr(spec_mod, "judge_dispute", lambda s, t, e, **kw: (1, "obsolete: subject removed"))
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "pivot goal"
    spec["tasks"] = [_disputed_task()]
    _apply_dispute(spec, spec["tasks"][0])
    assert spec["tasks"][0]["status"] == "retracted"


def test_obsolescence_rule_demands_absence_proof_not_mere_preference():
    # The rule must NOT be a blanket escape hatch: it explicitly rejects "merely
    # preferred a different approach" and requires captured absence proof.
    rule = spec_mod._DISPUTE_OBSOLETE_RULE.lower()
    assert "merely preferred" in rule
    assert "absence proof" in rule or "absent" in rule
