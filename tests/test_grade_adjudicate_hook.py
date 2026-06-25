#!/usr/bin/env python3
"""Tests for the judge-backed grade classifier manifest wiring and PostToolUse path.

The separate gate_prompt_grade_adjudicate.py hook has been removed; classification
now runs inside gate_prompt.py itself. These tests verify the manifests no longer
wire the deleted hook, and that the PostToolUse frontier path respects the
grade set by the prompt hook.
"""

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
from spec import save_spec  # noqa: E402

CLAUDE_MANIFEST = REPO / "hooks" / "hooks.json"
CODEX_MANIFEST = REPO / ".codex-plugin" / "hooks.json"
ADJUDICATE_SCRIPT = "gate_prompt_grade_adjudicate.py"
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
    def test_adjudicate_hook_removed_from_both_manifests(self) -> None:
        """The deleted gate_prompt_grade_adjudicate.py must not be wired."""
        for path in (CLAUDE_MANIFEST, CODEX_MANIFEST):
            data = json.loads(path.read_text())
            for group in data["hooks"].get("UserPromptSubmit", []):
                cmds = " ".join(h.get("command", "") for h in group.get("hooks", []))
                self.assertNotIn(ADJUDICATE_SCRIPT, cmds, f"{path.name} still wires the deleted hook")

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


class TestAdjudicateScriptDeleted(unittest.TestCase):
    def test_adjudicate_hook_script_does_not_exist(self) -> None:
        self.assertFalse((REPO / "hooks" / ADJUDICATE_SCRIPT).exists())


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
                    "grade": "STANDARD",
                    "frontier_research_tools": 5,
                    "frontier_discovery_count": 0,
                    "read_paths": ["hooks/gate_post_tool.py"],
                },
            )

            with patch("gate_post_tool.read_stdin_json", return_value=payload):
                with patch("spec_judge.judge_discover_frontiers") as discover:
                    rc = gate_post_tool.main()
            self.assertEqual(rc, 0)
            discover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
