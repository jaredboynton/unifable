#!/usr/bin/env python3
"""Assumptions are a hard gate rejection, not an accepted label.

A spec field carrying assumption/placeholder language ('assumption', 'assumed',
'presumably', 'tbd', ...) must fail validate_spec with a 'prove it' message;
real, proven evidence must pass. Guards the rule that uncited claims never
satisfy the gate.

Runs under pytest or standalone (python3 tests/test_assumption_reject.py).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))
from spec import check_fake_evidence, validate_spec  # noqa: E402


def _spec(evidence: str = "5 passed in 0.4s", why: str = "where routes register"):
    return {
        "restated_goal": "Add a health endpoint.",
        "acceptance_criteria": [{"check": "pytest -q", "evidence": evidence}],
        "repo_context": [{"cite": "src/app.py:10", "why": why}],
        "prior_art": [{"cite": "https://example.com/doc", "why": "fixture source"}],
    }


def _heavy_spec(evidence: str = "5 passed in 0.4s", why: str = "where routes register"):
    from spec import append_frontier_task, set_primary_task

    spec = _spec(evidence, why)
    spec["heavy_workflow"] = True
    spec["tasks"] = []
    append_frontier_task(spec, "F1", "true")
    append_frontier_task(spec, "F2", "true")
    set_primary_task(spec, "Primary", "true")
    return spec


def test_check_fake_evidence_flags_assumptions():
    for bad in ("(assumption)", "this is an assumption", "assumed to work", "presumably fine"):
        assert check_fake_evidence(bad), f"should flag: {bad!r}"
    assert not check_fake_evidence("5 passed in 0.4s")
    assert not check_fake_evidence("curl -s localhost/health -> 200")


def test_assumption_in_evidence_rejected():
    ok, reasons = validate_spec(_spec(evidence="(assumption) the endpoint returns 200"), "STANDARD", require_evidence=True)
    assert not ok
    assert any("prove it" in r.lower() for r in reasons), reasons


def test_assumption_in_repo_context_why_rejected():
    ok, reasons = validate_spec(_spec(why="presumably this is where it happens"), "STANDARD", require_evidence=True)
    assert not ok
    assert any("prove" in r.lower() or "assumption" in r.lower() for r in reasons), reasons


def test_proven_evidence_passes():
    ok, reasons = validate_spec(_heavy_spec(), "HEAVY", require_evidence=True)
    assert ok, reasons


if __name__ == "__main__":
    fails = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"  [OK] {_name}")
            except AssertionError as e:
                fails += 1
                print(f"  [FAIL] {_name}: {e}")
    print("RESULT:", "all pass" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
