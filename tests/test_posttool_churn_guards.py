#!/usr/bin/env python3
"""Anti-churn guards for the PostToolUse advisory judges:

  - spec_judge: a revise that lands on an already-applied (title, check) signature is
    a no-op (the cosmetic-reword loop fix), and per-task revises are capped.
  - posttool_notify: a 'Spec update:' block whose structural signature (task ids +
    action verbs) already surfaced this epoch is dropped, even when the reason text
    is paraphrased (which the full-body hash dedup misses).
"""

from __future__ import annotations

import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

from posttool_notify import _specupdate_signature, filter_spec_update  # noqa: E402
from spec_judge import (  # noqa: E402
    JUDGE_MAX_REVISES_PER_TASK,
    _apply_reconcile_actions,
)


def _task(tid: str = "T1") -> dict:
    return {"id": tid, "title": "Old title", "check": "pytest tests/x.py -q", "status": "failed", "added_by": "agent"}


def _revise(tid: str, *, title: str | None = None, check: str | None = None, reason: str, refs):
    action = {"action": "revise", "id": tid, "reason": reason, "evidence_refs": refs}
    if title is not None:
        action["title"] = title
    if check is not None:
        action["check"] = check
    return action


# --- idempotent revise ------------------------------------------------------


def test_revise_title_only_reword_is_noop_after_first_apply():
    """The session bug: the judge rewords the title every turn; the check is stable.
    First revise applies; a later revise that produces the SAME (title, check) is a
    no-op -- no status reset, no headline (so the churn loop cannot perpetuate)."""
    spec = {"requires_tasks": True, "tasks": [_task("T1")]}
    evidence = {"command_outputs": ["pytest tests/x.py -q -> the check is concrete now"]}

    first = _apply_reconcile_actions(
        spec,
        [_revise("T1", title="Clear concrete title", reason="state intent clearly", refs=["the check is concrete now"])],
        evidence=evidence,
    )
    assert first == ["T1 revised: state intent clearly"]
    assert spec["tasks"][0]["title"] == "Clear concrete title"
    spec["tasks"][0]["status"] = "validated"  # pretend it re-validated

    # Same resulting (title, check) -- only the reason is paraphrased: must be dropped.
    second = _apply_reconcile_actions(
        spec,
        [_revise("T1", title="Clear concrete title", reason="revising to state intent clearly", refs=["the check is concrete now"])],
        evidence=evidence,
    )
    assert second == []
    assert spec["tasks"][0]["status"] == "validated"  # not reset back to pending


def test_revise_genuine_check_change_still_applies():
    """A revise that actually repairs the CHECK is a real change (new signature) and
    must still apply even after a prior revise."""
    spec = {"requires_tasks": True, "tasks": [_task("T1")]}
    evidence = {"command_outputs": ["pytest tests/x.py -q -> moved", "pytest tests/y.py -q -> here"]}
    _apply_reconcile_actions(
        spec, [_revise("T1", check="pytest tests/x.py -q", reason="r1", refs=["moved"])], evidence=evidence
    )
    out = _apply_reconcile_actions(
        spec, [_revise("T1", check="pytest tests/y.py -q", reason="check moved to y", refs=["here"])], evidence=evidence
    )
    assert out == ["T1 revised: check moved to y"]
    assert spec["tasks"][0]["check"] == "pytest tests/y.py -q"


def test_revise_per_task_cap_bounds_churn():
    """Even when each revise nudges the signature, the per-task cap stops runaway
    churn after JUDGE_MAX_REVISES_PER_TASK distinct revisions."""
    spec = {"requires_tasks": True, "tasks": [_task("T1")]}
    applied = 0
    for i in range(JUDGE_MAX_REVISES_PER_TASK + 3):
        evidence = {"command_outputs": [f"pytest tests/v{i}.py -q -> ref{i}"]}
        out = _apply_reconcile_actions(
            spec,
            [_revise("T1", check=f"pytest tests/v{i}.py -q", reason=f"r{i}", refs=[f"ref{i}"])],
            evidence=evidence,
        )
        applied += len(out)
    assert applied == JUDGE_MAX_REVISES_PER_TASK


# --- structural Spec-update dedup ------------------------------------------


def test_specupdate_signature_ignores_reason_text():
    a = "Spec update:\nT3 revised: the title is slightly ambiguous; revise it"
    b = "Spec update:\nT3 revised: revising the title to clearly state the intent"
    assert _specupdate_signature(a) == _specupdate_signature(b) != ""


def test_specupdate_signature_distinguishes_different_tasks_and_verbs():
    revise_t3 = "Spec update:\nT3 revised: x"
    revise_t4 = "Spec update:\nT4 revised: x"
    retract_t3 = "Spec update:\nJudge retracted T3: x"
    assert _specupdate_signature(revise_t3) != _specupdate_signature(revise_t4)
    assert _specupdate_signature(revise_t3) != _specupdate_signature(retract_t3)


def test_filter_spec_update_drops_paraphrased_repeat():
    ledger: dict = {}
    first = "Spec update:\nT3 revised: the title is slightly ambiguous; revise it"
    kept = filter_spec_update(ledger, first)
    assert kept == first
    ledger["posttool_last_specupdate_sig"] = _specupdate_signature(first)
    repeat = "Spec update:\nT3 revised: revising the title to clearly state the intent"
    assert filter_spec_update(ledger, repeat) == ""


def test_filter_spec_update_keeps_unrecognized_block():
    ledger = {"posttool_last_specupdate_sig": "deadbeef"}
    block = "Spec update:\nsome free-form note with no headline"
    assert filter_spec_update(ledger, block) == block
