#!/usr/bin/env python3
"""Tests for judge-backed grade classification and HEAVY frontier discovery."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

from ledger import save_ledger  # noqa: E402
from spec import append_frontier_task, load_spec, save_spec  # noqa: E402

CLAUDE_MANIFEST = REPO / "hooks" / "hooks.json"
CODEX_MANIFEST = REPO / ".codex-plugin" / "hooks.json"
POST_HOOK = REPO / "hooks" / "gate_post_tool.py"
PY = sys.executable

VALID_SPEC = {
    "restated_goal": "Fix grade adjudication.",
    "goal_seeded": False,
    "acceptance_criteria": [{"check": "pytest -q", "evidence": "5 passed in 0.4s"}],
    "repo_context": [{"cite": "hooks/pre_tool_use.py:1", "why": "pre-edit gate entry"}],
    "prior_art": [{"cite": "https://example.com/doc", "why": "fixture source"}],
    "tasks": [],
}


class TestManifestWiring(unittest.TestCase):
    def test_gate_prompt_is_wired_for_user_prompt_submit(self) -> None:
        """The prompt pipeline uses gate_prompt.py for user-prompt grading."""
        for path in (CLAUDE_MANIFEST, CODEX_MANIFEST):
            data = json.loads(path.read_text())
            hooks = [
                hook
                for group in data["hooks"].get("UserPromptSubmit", [])
                for hook in group.get("hooks", [])
                if "gate_prompt.py" in hook.get("command", "")
            ]
            self.assertTrue(hooks, f"{path.name} is missing gate_prompt.py")

    def test_gate_prompt_has_judge_timeout(self) -> None:
        """gate_prompt.py needs the 95s timeout since it now calls the judge."""
        for path in (CLAUDE_MANIFEST, CODEX_MANIFEST):
            data = json.loads(path.read_text())
            for group in data["hooks"].get("UserPromptSubmit", []):
                cmds = " ".join(h.get("command", "") for h in group.get("hooks", []))
                if "gate_prompt.py" in cmds:
                    timeout = max(h.get("timeout", 0) for h in group.get("hooks", []))
                    self.assertGreaterEqual(timeout, 90, f"{path.name} gate_prompt timeout too low")
                    break


class TestPostToolAdjudicateBeforeDiscovery(unittest.TestCase):
    def test_downgrade_skips_frontier_discovery(self) -> None:
        """When the grade is STANDARD (not HEAVY), frontier discovery must not
        fire regardless of research tool count."""
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
            save_ledger(
                payload,
                {
                    "active_task": "k",
                    "task_mode": "normal",
                    "frontier_research_tools": 5,
                    "frontier_discovery_count": 0,
                    "read_paths": ["hooks/gate_post_tool.py"],
                },
            )

            with patch("gate_post_tool.read_stdin_json", return_value=payload):
                with patch("evidence_policy.resolve_grade", return_value="STANDARD"):
                    with patch("spec_judge.judge_discover_frontiers") as discover:
                        rc = gate_post_tool.main()
            self.assertEqual(rc, 0)
            discover.assert_not_called()

    def test_heavy_grade_discovers_frontier_and_emits_spec_update(self) -> None:
        """At the HEAVY threshold, PostToolUse adds frontier work and emits it."""
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
            save_ledger(
                payload,
                {
                    "active_task": "k",
                    "task_mode": "normal",
                    "frontier_research_tools": 2,
                    "frontier_discovery_count": 0,
                    "read_paths": ["hooks/gate_post_tool.py"],
                },
            )

            def fake_discover(spec_obj, _activity):
                return [
                    append_frontier_task(
                        spec_obj,
                        "Zero-copy mmap",
                        "pytest tests/test_mmap.py -q",
                        added_by="judge",
                        scope_paths=["src/parser.py"],
                    )
                ]

            with patch("gate_post_tool.read_stdin_json", return_value=payload):
                with patch("evidence_policy.resolve_grade", return_value="HEAVY"):
                    with patch("spec_judge.judge_reconcile_spec", return_value=[]):
                        with patch("spec_judge.judge_discover_frontiers", side_effect=fake_discover):
                            with patch("posttool_notify.emit_json") as emit:
                                rc = gate_post_tool.main()

            self.assertEqual(rc, 0)
            updated = load_spec(cwd, "sess")
            self.assertEqual(len(updated["tasks"]), 1)
            self.assertEqual(updated["tasks"][0]["approach_kind"], "frontier")
            ctx = (emit.call_args[0][0].get("hookSpecificOutput") or {}).get("additionalContext") or ""
            self.assertIn("Judge added frontier approach(s): T1.", ctx)
            self.assertIn("Zero-copy mmap", ctx)
            self.assertIn("Explore ALL frontiers thoroughly", ctx)


if __name__ == "__main__":
    unittest.main()
