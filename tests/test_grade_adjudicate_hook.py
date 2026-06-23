#!/usr/bin/env python3
"""Tests for proactive HEAVY grade adjudication hooks and integration paths."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

from evidence_policy import resolve_grade  # noqa: E402
from ledger import load_ledger, save_ledger  # noqa: E402
from spec import save_spec  # noqa: E402

HOOK = REPO / "hooks" / "gate_prompt_grade_adjudicate.py"
POST_HOOK = REPO / "hooks" / "gate_post_tool.py"
PRE_HOOK = REPO / "hooks" / "pre_tool_use.py"
PY = sys.executable

VALID_SPEC = {
    "restated_goal": "Fix grade adjudication.",
    "goal_seeded": False,
    "acceptance_criteria": [{"check": "pytest -q", "evidence": "5 passed in 0.4s"}],
    "repo_context": [{"cite": "hooks/pre_tool_use.py:1", "why": "pre-edit gate entry"}],
    "prior_art": [{"cite": "https://example.com/doc", "why": "fixture source"}],
    "tasks": [],
}


class TestAdjudicateHookSubprocess(unittest.TestCase):
    def test_non_heavy_emits_empty(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir:
            os.environ["UNIFABLE_DATA"] = data_dir
            payload = {
                "prompt": "implement the fix",
                "session_id": "s1",
                "cwd": "/tmp",
            }
            save_ledger(payload, {"active_task": "k", "task_mode": "normal", "grade": "STANDARD"})
            proc = subprocess.run(
                [PY, str(HOOK)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout or "{}"), {})


class TestGatePromptPin(unittest.TestCase):
    def test_pinned_grade_survives_deep_classification(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
            os.environ["UNIFABLE_DATA"] = data_dir
            payload = {
                "prompt": "production-ready auth migration for payments",
                "session_id": "sess",
                "cwd": cwd,
            }
            save_ledger(payload, {
                "active_task": "old",
                "task_mode": "normal",
                "grade": "STANDARD",
                "grade_override_applied": True,
                "grade_override_target": "STANDARD",
            })
            proc = subprocess.run(
                [PY, str(REPO / "hooks" / "gate_prompt.py")],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
                env={**os.environ, "UNIFABLE_DATA": data_dir, "UNIFABLE_VERIFY_CITATIONS": "0"},
            )
            self.assertEqual(proc.returncode, 0)
            ledger = load_ledger(payload)
            self.assertEqual(ledger["task_mode"], "normal")
            self.assertEqual(resolve_grade(ledger), "STANDARD")


class TestPreToolUsePinnedGrade(unittest.TestCase):
    def test_pinned_standard_allows_write_without_frontier(self) -> None:
        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
            os.environ["UNIFABLE_DATA"] = data_dir
            os.makedirs(os.path.join(cwd, "src"), exist_ok=True)
            target = os.path.join(cwd, "src", "fix.py")
            Path(target).write_text("x\n", encoding="utf-8")
            payload = {
                "tool_name": "Edit",
                "tool_input": {"file_path": target, "old_string": "x", "new_string": "y"},
                "session_id": "sess-pin",
                "cwd": cwd,
            }
            save_ledger(payload, {
                "active_task": "abc",
                "task_mode": "deep",
                "grade": "HEAVY",
                "grade_override_applied": True,
                "grade_override_target": "STANDARD",
            })
            save_spec(cwd, "sess-pin", dict(VALID_SPEC))
            env = dict(os.environ)
            env["UNIFABLE_DATA"] = data_dir
            env.pop("UNIFABLE_GRADE", None)
            env["UNIFABLE_VERIFY_CITATIONS"] = "0"
            proc = subprocess.run(
                [PY, str(PRE_HOOK)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)


class TestPostToolAdjudicateBeforeDiscovery(unittest.TestCase):
    def test_downgrade_skips_frontier_discovery(self) -> None:
        import gate_post_tool

        with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
            os.environ["UNIFABLE_DATA"] = data_dir
            spec = dict(VALID_SPEC)
            spec["heavy_workflow"] = True
            save_spec(cwd, "sess", spec)
            payload = {
                "tool_name": "Read",
                "tool_input": {"path": "hooks/gate_post_tool.py"},
                "tool_response": {"success": True},
                "session_id": "sess",
                "cwd": cwd,
            }
            save_ledger(payload, {
                "active_task": "k",
                "task_mode": "deep",
                "grade": "HEAVY",
                "frontier_research_tools": 2,
                "frontier_discovery_count": 0,
                "read_paths": ["hooks/gate_post_tool.py"],
            })

            def fake_adjudicate(_input, _prompt, **_kw):
                def apply(ld):
                    from grade_override import apply_grade_override_ledger

                    apply_grade_override_ledger(ld, "normal", "harness fix", by="judge")

                from ledger import update_ledger

                update_ledger(_input, apply)
                return "downgraded"

            with patch("gate_post_tool.read_stdin_json", return_value=payload):
                with patch("grade_override.try_adjudicate_grade", side_effect=fake_adjudicate):
                    with patch("spec.judge_discover_frontiers") as discover:
                        rc = gate_post_tool.main()
            self.assertEqual(rc, 0)
            discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
