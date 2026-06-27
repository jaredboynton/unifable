#!/usr/bin/env python3
"""Regression: load-bearing task title/reason/rationale in Spec-update headlines
must NOT be hard-truncated, and a multi-task reconcile must not be silently
dropped past a count cap.

Live failure: the PostToolUse hook injected
  "Spec update: Judge added T9: Tokenizer counts are captured from fresh
   rendered transcript artifacts for both"
-- the task title was cut at 80 chars mid-sentence, so the main model never
learned what T9 actually was. The full title lived in the spec, but the model
only sees the headline. The same [:80] cap hit retraction reasons and frontier
selection rationales; a separate headlines[:4] cap dropped any reconcile
headline beyond the fourth. These are load-bearing task definitions, not
display chrome, so they must reach the model whole.
"""

from __future__ import annotations

import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
HOOKS = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(GATE))
sys.path.insert(0, str(HOOKS))

from spec import (  # noqa: E402
    _apply_reconcile_actions,
    append_frontier_task,
    judge_frontier_comparison,
)
from spec_judge import (  # noqa: E402
    _add_judge_requirement,
    build_spec_update_context,
)


def _spec_with_one_task() -> dict:
    return {
        "requires_tasks": True,
        "restated_goal": "g",
        "tasks": [
            {"id": "T1", "title": "seed", "check": "true", "status": "failed", "added_by": "agent"}
        ],
    }


LONG_TITLE = (
    "Tokenizer counts are captured from fresh rendered transcript artifacts "
    "for both headtail and mask strategies"
)
assert len(LONG_TITLE) > 80

LONG_REASON = (
    "The captured transcript artifact proves the old counting path is gone "
    "because the renderer now emits rendered spans instead of raw token spans "
    "across every compression strategy the judge exercised."
)
assert len(LONG_REASON) > 80

LONG_RATIONALE = (
    "Frontier T2 wins because it captures tokenizer counts from fresh rendered "
    "transcript artifacts under both headtail and mask strategies, while T3 "
    "re-renders on every call and busts the prompt cache the judge relies on."
)
assert len(LONG_RATIONALE) > 80


def test_judge_added_headline_keeps_full_long_title():
    spec = _spec_with_one_task()
    headlines = _add_judge_requirement(
        spec,
        title=LONG_TITLE,
        check="rg tokenizer scripts/gate/transcript_tail.py",
        reason="renderer changed",
        evidence_refs=["rendered artifact"],
    )
    assert headlines, "expected one 'Judge added' headline"
    added = [h for h in headlines if h.startswith("Judge added ")]
    assert added, f"expected a 'Judge added' headline, got {headlines}"
    # The full title must survive -- the tail word past the old 80-char cut is the proof.
    assert LONG_TITLE in added[0]
    assert "strategies" in added[0]
    assert len(added[0]) > 80 + len("Judge added T2: ")
    # And the spec stores the full title too.
    new_task = next(t for t in spec["tasks"] if t.get("added_by") == "judge")
    assert new_task["title"] == LONG_TITLE


def test_reconcile_retract_headline_keeps_full_long_reason():
    spec = _spec_with_one_task()
    evidence = {"command_outputs": ["rg old-route src -> 0 matches; old route was removed"]}
    actions = [
        {
            "action": "retract",
            "id": "T1",
            "reason": LONG_REASON,
            "evidence_refs": ["rg old-route src -> 0 matches"],
        }
    ]
    headlines = _apply_reconcile_actions(spec, actions, evidence=evidence)
    assert headlines == [f"Judge retracted T1: {LONG_REASON}"]
    assert "renderer now emits rendered spans" in headlines[0]


def test_frontier_selection_headline_keeps_full_long_rationale(monkeypatch):
    import judge_transport

    spec = {
        "requires_tasks": True,
        "heavy_workflow": True,
        "restated_goal": "Pick the tokenizer-count strategy",
        "tasks": [],
    }
    append_frontier_task(spec, "headtail+mask render", "true", added_by="agent")
    append_frontier_task(spec, "re-render every call", "true", added_by="agent")
    # Mark one accepted so judge_frontier_comparison proceeds.
    spec["tasks"][0]["status"] = "accepted_approach"
    spec["tasks"][1]["status"] = "accepted_approach"

    def fake_ask(_system, _user, _schema, schema_name=""):
        assert schema_name == "frontier_comparison"
        return {"selected_id": "T1", "selection_rationale": LONG_RATIONALE}

    monkeypatch.setattr(judge_transport, "ask_structured", fake_ask)
    headlines = judge_frontier_comparison(spec)
    sel = [h for h in headlines if "selected as best frontier" in h]
    assert sel, f"expected a selection headline, got {headlines}"
    assert LONG_RATIONALE in sel[0]
    assert "prompt cache the judge relies on" in sel[0]


def test_build_spec_update_context_keeps_every_headline_past_old_count_cap():
    # The old code did "\n".join(headlines[:4]); a 5+ task reconcile silently
    # dropped everything past the fourth headline. Build a reconcile that yields
    # six headlines and assert ALL of them reach the context.
    headlines = [f"Judge retracted T{i}: reason {i} is long enough to matter" for i in range(1, 7)]
    ctx = build_spec_update_context(headlines)
    assert ctx.startswith("Spec update:\n")
    for h in headlines:
        assert h in ctx
    # Specifically the sixth headline -- the one the old [:4] cap dropped.
    assert "Judge retracted T6:" in ctx


def test_build_spec_update_context_empty_when_nothing_to_report():
    assert build_spec_update_context([]) == ""
    assert build_spec_update_context(["", "  "]) == ""


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
