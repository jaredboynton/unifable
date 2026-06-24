#!/usr/bin/env python3
"""Predicate self-verify (Approach A): the arm judge may emit a falsifiable
predicate {must_contain, must_not_contain} over repo files; the breaker runs it
read-only and DOWNGRADES the arm to allow only when the files CONFIRM the claim.

This is de-escalation only: confirmed -> do not arm; refuted/unverifiable ->
behave exactly as before (arm on verdict 1). A buggy or empty predicate can never
introduce a new block, only remove a false one.
"""

from __future__ import annotations

import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
if str(GATE) not in sys.path:
    sys.path.insert(0, str(GATE))

import groundedness as g  # noqa: E402


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --- verify_claim_predicate: the deterministic core --------------------------


def test_predicate_confirmed_when_files_match(tmp_path):
    _write(tmp_path, "a/plugin.json", '{"version": "1.9.90"}')
    _write(tmp_path, "b/marketplace.json", '{"version": "1.9.90"}')
    pred = {
        "must_contain": [
            {"file": "a/plugin.json", "text": "1.9.90"},
            {"file": "b/marketplace.json", "text": "1.9.90"},
        ],
        "must_not_contain": [
            {"file": "a/plugin.json", "text": "1.9.89"},
        ],
    }
    assert g.verify_claim_predicate(pred, str(tmp_path)) == "confirmed"


def test_predicate_refuted_when_must_contain_missing(tmp_path):
    _write(tmp_path, "a/plugin.json", '{"version": "1.9.89"}')  # still old
    pred = {"must_contain": [{"file": "a/plugin.json", "text": "1.9.90"}], "must_not_contain": []}
    assert g.verify_claim_predicate(pred, str(tmp_path)) == "refuted"


def test_predicate_refuted_when_forbidden_text_present(tmp_path):
    _write(tmp_path, "a/plugin.json", '{"version": "1.9.90"}\n# leftover 1.9.89')
    pred = {
        "must_contain": [{"file": "a/plugin.json", "text": "1.9.90"}],
        "must_not_contain": [{"file": "a/plugin.json", "text": "1.9.89"}],
    }
    assert g.verify_claim_predicate(pred, str(tmp_path)) == "refuted"


def test_predicate_unverifiable_when_file_missing(tmp_path):
    pred = {"must_contain": [{"file": "nope.json", "text": "x"}], "must_not_contain": []}
    assert g.verify_claim_predicate(pred, str(tmp_path)) == "unverifiable"


def test_predicate_unverifiable_when_empty(tmp_path):
    assert g.verify_claim_predicate({"must_contain": [], "must_not_contain": []}, str(tmp_path)) == "unverifiable"
    assert g.verify_claim_predicate(None, str(tmp_path)) == "unverifiable"


def test_predicate_unverifiable_on_path_escape(tmp_path):
    # An absolute path outside cwd must never be trusted (read-only containment).
    outside = tmp_path.parent / "outside.json"
    outside.write_text("1.9.90", encoding="utf-8")
    pred = {"must_contain": [{"file": str(outside), "text": "1.9.90"}], "must_not_contain": []}
    assert g.verify_claim_predicate(pred, str(tmp_path)) == "unverifiable"


# --- arm_judge integration: de-escalation only -------------------------------


def _stub(verify):
    def judge(system, user, schema):
        return {
            "load_bearing": 1,
            "verdict": 1,
            "claim": "All plugin manifests declare version 1.9.90 and none still declare 1.9.89.",
            "steering": "Read the manifests to confirm.",
            "verify": verify,
        }

    return judge


def test_arm_judge_downgrades_when_files_confirm(tmp_path):
    _write(tmp_path, "a/plugin.json", '{"version": "1.9.90"}')
    pred = {
        "must_contain": [{"file": "a/plugin.json", "text": "1.9.90"}],
        "must_not_contain": [{"file": "a/plugin.json", "text": "1.9.89"}],
    }
    verdict, steering, claim = g.arm_judge(
        "model is about to commit", events=[], judge=_stub(pred), input_data={"cwd": str(tmp_path)}
    )
    assert verdict == 0
    assert steering == "" and claim == ""


def test_arm_judge_still_arms_when_files_refute(tmp_path):
    _write(tmp_path, "a/plugin.json", '{"version": "1.9.89"}')  # claim is false
    pred = {"must_contain": [{"file": "a/plugin.json", "text": "1.9.90"}], "must_not_contain": []}
    verdict, steering, claim = g.arm_judge(
        "model is about to commit", events=[], judge=_stub(pred), input_data={"cwd": str(tmp_path)}
    )
    assert verdict == 1
    assert claim  # the original claim is preserved so the model is told what failed


def test_arm_judge_unchanged_without_predicate(tmp_path):
    # No verify predicate -> behaves exactly as before (arms on verdict 1).
    verdict, steering, claim = g.arm_judge(
        "model is about to commit", events=[], judge=_stub(None), input_data={"cwd": str(tmp_path)}
    )
    assert verdict == 1
    assert claim
