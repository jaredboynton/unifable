#!/usr/bin/env python3
"""Tests for operative classification and judge-backed HEAVY override."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

from classify_task import (  # noqa: E402
    OPS_PROSE_RE,
    classify_prompt,
    operative_prompt,
)
from evidence_policy import resolve_grade  # noqa: E402
from grade_override import (  # noqa: E402
    apply_grade_override_ledger,
    apply_grade_override_to_spec,
    clear_heavy_spec_fields,
    try_apply_grade_override,
)
from spec import load_spec, save_spec, spec_template  # noqa: E402

HOOK = REPO / "hooks" / "gate_prompt_grade_override.py"
PY = sys.executable


class TestOperativePrompt(unittest.TestCase):
    def test_extracts_after_user_marker(self) -> None:
        full = "pasted table production pilot\n❯ get dispatch send-out-ready"
        self.assertEqual(operative_prompt(full), "get dispatch send-out-ready")

    def test_pasted_production_does_not_force_deep(self) -> None:
        corpus = "production spec-first " * 200
        ask = "cache the dispatch and iterate send-out-ready"
        full = f"{corpus}\n❯ {ask}"
        mode, _ = classify_prompt(full)
        self.assertEqual(mode, "normal")
        self.assertNotEqual(mode, "deep")

    def test_ops_prose_caps_deep(self) -> None:
        text = "Update the CSE customer dispatch roll-up for exec review"
        mode, _ = classify_prompt(text)
        self.assertEqual(mode, "normal")
        self.assertTrue(OPS_PROSE_RE.search(text))

    def test_production_in_operative_still_deep(self) -> None:
        text = "production-ready auth migration for payments"
        mode, risks = classify_prompt(text)
        self.assertEqual(mode, "deep")
        self.assertIn("production", risks)


class TestResolveGradeOverride(unittest.TestCase):
    def test_override_beats_sticky_deep(self) -> None:
        ledger = {
            "active_task": "abc",
            "task_mode": "normal",
            "grade_override_applied": True,
            "grade": "HEAVY",
        }
        self.assertEqual(resolve_grade(ledger), "STANDARD")


class TestGradeOverrideApply(unittest.TestCase):
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

    def test_apply_grade_override_ledger(self) -> None:
        ledger: dict = {"task_mode": "deep", "grade": "HEAVY"}
        apply_grade_override_ledger(ledger, "normal", "operator prose task")
        self.assertEqual(ledger["task_mode"], "normal")
        self.assertEqual(ledger["grade"], "STANDARD")
        self.assertTrue(ledger["grade_override_applied"])


class TestTryApplyGradeOverride(unittest.TestCase):
    def test_mock_judge_applies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data_dir:
            os.environ["UNIFABLE_DATA"] = str(data_dir)
            spec = spec_template()
            spec["heavy_workflow"] = True
            spec["restated_goal"] = "Ship dispatch"
            save_spec(tmp, "sess", spec)

            def fake_judge(*_a, **_k):
                return {
                    "apply_override": True,
                    "target_mode": "normal",
                    "reason": "prose dispatch task",
                }

            payload = {
                "prompt": "manual override of heavy -- this is a NORMAL task",
                "session_id": "sess",
                "cwd": tmp,
            }
            ctx = try_apply_grade_override(payload, payload["prompt"], judge_fn=fake_judge)
            self.assertIn("HEAVY lifted", ctx)
            loaded = load_spec(tmp, "sess")
            assert loaded is not None
            self.assertFalse(loaded.get("heavy_workflow"))

    def test_judge_failure_fail_open(self) -> None:
        def boom(*_a, **_k):
            return None

        payload = {"prompt": "manual override of heavy", "session_id": "s", "cwd": "/tmp"}
        self.assertEqual(try_apply_grade_override(payload, payload["prompt"], judge_fn=boom), "")


class TestOverrideHook(unittest.TestCase):
    def test_hook_fail_open_on_error(self) -> None:
        proc = subprocess.run(
            [PY, str(HOOK)],
            input="not json",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout or "{}"), {})


if __name__ == "__main__":
    unittest.main()
