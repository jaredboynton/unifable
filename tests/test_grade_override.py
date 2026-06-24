#!/usr/bin/env python3
"""Tests for the judge-backed grade classifier and ledger/spec application."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

from classify_task import operative_prompt  # noqa: E402
from evidence_policy import resolve_grade  # noqa: E402
from grade_override import (  # noqa: E402
    apply_classified_grade_ledger,
    apply_grade_override_to_spec,
    clear_heavy_spec_fields,
    format_override_context,
    judge_grade_classify,
    parse_grade_verdict,
)
from spec import load_spec, save_spec, spec_template  # noqa: E402


class TestOperativePrompt(unittest.TestCase):
    def test_extracts_after_user_marker(self) -> None:
        full = "pasted table production pilot\n> get dispatch send-out-ready"
        self.assertEqual(operative_prompt(full), "get dispatch send-out-ready")


class TestResolveGradeOverride(unittest.TestCase):
    def test_override_beats_sticky_deep(self) -> None:
        ledger = {
            "active_task": "abc",
            "task_mode": "normal",
            "grade_override_applied": True,
            "grade_override_target": "STANDARD",
            "grade": "HEAVY",
        }
        self.assertEqual(resolve_grade(ledger), "STANDARD")


class TestGradeApply(unittest.TestCase):
    def test_clear_heavy_spec_fields(self) -> None:
        spec = {"heavy_workflow": True, "heavy_phase": "frontier"}
        clear_heavy_spec_fields(spec)
        self.assertFalse(spec["heavy_workflow"])
        self.assertNotIn("heavy_phase", spec)

    def test_apply_grade_override_to_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = spec_template()
            spec["heavy_workflow"] = True
            spec["heavy_phase"] = "declare"
            save_spec(tmp, "sess", spec)
            self.assertTrue(apply_grade_override_to_spec(tmp, "sess"))
            loaded = load_spec(tmp, "sess")
            assert loaded is not None
            self.assertFalse(loaded.get("heavy_workflow"))

    def test_apply_classified_grade_ledger(self) -> None:
        ledger: dict = {"task_mode": "deep", "grade": "HEAVY"}
        apply_classified_grade_ledger(ledger, "normal", "bounded fix", by="judge")
        self.assertEqual(ledger["task_mode"], "normal")
        self.assertEqual(ledger["grade"], "STANDARD")
        self.assertEqual(ledger["grade_override_target"], "STANDARD")
        self.assertEqual(ledger["grade_override_by"], "judge")
        self.assertTrue(ledger["grade_override_applied"])
        self.assertEqual(ledger["evidence_profile"], "code")

    def test_apply_classified_grade_ledger_operational_profile(self) -> None:
        ledger: dict = {}
        apply_classified_grade_ledger(ledger, "normal", "account research", by="judge", evidence_profile="operational")
        self.assertEqual(ledger["evidence_profile"], "operational")

    def test_format_override_context(self) -> None:
        ctx = format_override_context("normal", "bounded fix", by="judge")
        self.assertIn("normal", ctx)
        self.assertIn("STANDARD", ctx)
        self.assertIn("bounded fix", ctx)


class TestJudgeClassifyFailOpen(unittest.TestCase):
    def test_empty_operative_returns_none(self) -> None:
        self.assertIsNone(judge_grade_classify(""))

    def test_judge_fn_exception_returns_none(self) -> None:
        def boom(**kw):
            raise RuntimeError("down")

        self.assertIsNone(judge_grade_classify("fix bug", judge_fn=boom))

    def test_parse_none_falls_to_normal(self) -> None:
        mode, flags, reason, profile = parse_grade_verdict(None)
        self.assertEqual(mode, "normal")
        self.assertEqual(flags, [])
        self.assertEqual(reason, "")
        self.assertEqual(profile, "code")


if __name__ == "__main__":
    unittest.main()
