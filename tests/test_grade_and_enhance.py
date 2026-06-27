#!/usr/bin/env python3
"""_grade_and_enhance in hooks/gate_prompt.py -- concurrent grade + enhance.

Verifies the UserPromptSubmit wiring (ThreadPoolExecutor fan-out, post-grade
use gate, fail-open on either leg) WITHOUT a live judge or Node subprocess:
both legs are monkeypatched. This is the deterministic companion to the
entrypoint smoke (skills/explore/scripts/enhance-prompt.mjs) and the
gate-robustness fail-open checks.

Run: python3 -m pytest tests/test_grade_and_enhance.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "gate"))
sys.path.insert(0, str(ROOT / "hooks"))

import gate_prompt as gp  # noqa: E402

VAGUE = (
    "something is off with how the user prompt submit hook assembles the mode "
    "context the model keeps getting weird weak verification guidance diagnose and fix it"
)
PATHED = "fix the off-by-one in lib/pagination.ts slice helper around the cursor clamp"
ENH = {
    "enhanced_prompt": "Investigate lib/foo.mjs:10-20 and run a test that exercises it.",
    "cited_ranges": ["lib/foo.mjs:10-20"],
}


def _grade(profile="code", mode="normal", risks=None, reason="r"):
    def _stub(operative, prior_spec):
        _stub.calls.append(operative)
        return mode, risks or [], reason, profile

    _stub.calls = []
    return _stub


def _enhancer(ret=ENH, exc=None):
    def _stub(prompt, cwd):
        _stub.calls.append((prompt, cwd))
        if exc is not None:
            raise exc
        return ret

    _stub.calls = []
    return _stub


def test_grade_and_enhance_injects_when_code_normal(monkeypatch):
    grade = _grade(profile="code", mode="normal")
    enh = _enhancer()
    monkeypatch.setattr(gp, "_classify_prompt_grade", grade)
    monkeypatch.setattr(gp, "run_enhancer", enh)
    mode, risks, reason, profile, line = gp._grade_and_enhance(VAGUE, None, VAGUE, "/tmp")
    assert mode == "normal" and profile == "code"
    assert line and "lib/foo.mjs" in line
    assert len(grade.calls) == 1 and len(enh.calls) == 1  # both legs ran


def test_grade_and_enhance_post_grade_gate_discards_operational(monkeypatch):
    grade = _grade(profile="operational", mode="normal")
    enh = _enhancer()
    monkeypatch.setattr(gp, "_classify_prompt_grade", grade)
    monkeypatch.setattr(gp, "run_enhancer", enh)
    _, _, _, profile, line = gp._grade_and_enhance(VAGUE, None, VAGUE, "/tmp")
    assert profile == "operational"
    assert line is None  # enhancer ran but post-grade gate discarded


def test_grade_and_enhance_post_grade_gate_discards_quick(monkeypatch):
    grade = _grade(profile="code", mode="quick")
    enh = _enhancer()
    monkeypatch.setattr(gp, "_classify_prompt_grade", grade)
    monkeypatch.setattr(gp, "run_enhancer", enh)
    _, _, _, _, line = gp._grade_and_enhance(VAGUE, None, VAGUE, "/tmp")
    assert line is None


def test_grade_and_enhance_grade_failopen_keeps_enhance(monkeypatch):
    def _raise(operative, prior_spec):
        raise RuntimeError("judge down")

    enh = _enhancer()
    monkeypatch.setattr(gp, "_classify_prompt_grade", _raise)
    monkeypatch.setattr(gp, "run_enhancer", enh)
    mode, risks, reason, profile, line = gp._grade_and_enhance(VAGUE, None, VAGUE, "/tmp")
    assert mode == "normal" and profile == "code"  # grade fail-open default
    assert line and "lib/foo.mjs" in line  # enhance still injected (profile defaulted to code)


def test_grade_and_enhance_enhance_failopen(monkeypatch):
    grade = _grade(profile="code", mode="normal")
    enh = _enhancer(exc=RuntimeError("node missing"))
    monkeypatch.setattr(gp, "_classify_prompt_grade", grade)
    monkeypatch.setattr(gp, "run_enhancer", enh)
    mode, risks, reason, profile, line = gp._grade_and_enhance(VAGUE, None, VAGUE, "/tmp")
    assert mode == "normal" and profile == "code"
    assert line is None  # enhance failed open -> static baseline


def test_grade_and_enhance_skips_subprocess_when_fire_false(monkeypatch):
    grade = _grade(profile="code", mode="normal")
    enh = _enhancer()
    monkeypatch.setattr(gp, "_classify_prompt_grade", grade)
    monkeypatch.setattr(gp, "run_enhancer", enh)
    # pathed operative -> fire_enhance False -> run_enhancer never called
    _, _, _, _, line = gp._grade_and_enhance(PATHED, None, PATHED, "/tmp")
    assert line is None
    assert enh.calls == []


def test_grade_and_enhance_enhancer_returns_none(monkeypatch):
    grade = _grade(profile="code", mode="normal")
    enh = _enhancer(ret=None)
    monkeypatch.setattr(gp, "_classify_prompt_grade", grade)
    monkeypatch.setattr(gp, "run_enhancer", enh)
    _, _, _, _, line = gp._grade_and_enhance(VAGUE, None, VAGUE, "/tmp")
    assert line is None
