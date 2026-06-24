#!/usr/bin/env python3
"""Tests for hooks/gate_prompt_effort.py — effort-gated playbook injection.

Coverage:
  (a) Heavy effort -> injects once; second call same session -> empty {}
  (b) Non-heavy effort -> empty {}
  (c) Effort in top-level string form (bare string, not dict)
  (d) Effort in nested dict form (effort.level)
  (e) Effort from CLAUDE_EFFORT env var
  (f) Effort from UNIFABLE_EFFORT env var
  (g) Empty / malformed stdin -> empty {} (fail open)

All marker files land in a per-test tmpdir set via UNIFABLE_MARKER_DIR so tests
are isolated and never interfere with real session markers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "hooks", "gate_prompt_effort.py")
PY = sys.executable


def run_hook(payload: dict, *, env_extra: dict | None = None,
             marker_dir: str | None = None) -> dict:
    """Run gate_prompt_effort.py with a JSON payload on stdin.

    Returns the parsed stdout dict; returns {} on parse failure.
    """
    env = dict(os.environ)
    if marker_dir:
        env["UNIFABLE_MARKER_DIR"] = marker_dir
    if env_extra:
        env.update(env_extra)
    # Strip any effort env vars that might leak in from the parent process.
    env.pop("CLAUDE_EFFORT", None)
    env.pop("UNIFABLE_EFFORT", None)
    if env_extra:
        # Re-apply after the strip so explicit test values take effect.
        env.update(env_extra)

    p = subprocess.run(
        [PY, HOOK],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    assert p.returncode == 0, f"hook exited {p.returncode}: {p.stderr}"
    try:
        return json.loads(p.stdout.strip() or "{}")
    except json.JSONDecodeError:
        return {"_raw": p.stdout, "_err": p.stderr}


def _is_injection(result: dict) -> bool:
    return bool(
        result.get("hookSpecificOutput", {}).get("additionalContext")
    )


class TestEffortInject(unittest.TestCase):

    def setUp(self):
        # Each test gets its own marker dir so sessions never bleed across tests.
        self._tmpdir = tempfile.mkdtemp(prefix="unifable_effort_test_")
        self._marker_dir = self._tmpdir

    # ------------------------------------------------------------------
    # (a) Heavy effort path
    # ------------------------------------------------------------------

    def test_heavy_effort_injects_on_first_call(self):
        result = run_hook(
            {"session_id": "sess-heavy-001", "prompt": "do something", "effort": "xhigh"},
            marker_dir=self._marker_dir,
        )
        self.assertTrue(_is_injection(result),
                        f"Expected injection, got: {result}")

    def test_heavy_effort_dedup_second_call_returns_empty(self):
        payload = {"session_id": "sess-heavy-002", "prompt": "do something", "effort": "max"}
        run_hook(payload, marker_dir=self._marker_dir)  # first call
        result = run_hook(payload, marker_dir=self._marker_dir)  # second call same session
        self.assertEqual(result, {},
                         f"Expected empty dict on second call, got: {result}")

    def test_different_sessions_inject_independently(self):
        r1 = run_hook(
            {"session_id": "sess-a", "prompt": "x", "effort": "ultracode"},
            marker_dir=self._marker_dir,
        )
        r2 = run_hook(
            {"session_id": "sess-b", "prompt": "x", "effort": "ultracode"},
            marker_dir=self._marker_dir,
        )
        self.assertTrue(_is_injection(r1), "sess-a first call should inject")
        self.assertTrue(_is_injection(r2), "sess-b first call should inject (different session)")

    def test_all_heavy_effort_values_inject(self):
        for i, effort in enumerate(["xhigh", "max", "ultracode"]):
            sid = f"sess-all-{i}"
            result = run_hook(
                {"session_id": sid, "prompt": "x", "effort": effort},
                marker_dir=self._marker_dir,
            )
            with self.subTest(effort=effort):
                self.assertTrue(_is_injection(result), f"{effort!r} should inject")

    # ------------------------------------------------------------------
    # (b) Non-heavy effort -> no injection
    # ------------------------------------------------------------------

    def test_non_heavy_effort_returns_empty(self):
        for effort in ["low", "medium", "high", "normal", "", "default"]:
            result = run_hook(
                {"session_id": f"sess-light-{effort}", "prompt": "x", "effort": effort},
                marker_dir=self._marker_dir,
            )
            with self.subTest(effort=effort):
                self.assertEqual(result, {},
                                 f"effort={effort!r} should produce empty dict")

    def test_no_effort_field_returns_empty(self):
        result = run_hook(
            {"session_id": "sess-noeffort", "prompt": "just a normal prompt"},
            marker_dir=self._marker_dir,
        )
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # (c) Nested dict form: effort.level
    # ------------------------------------------------------------------

    def test_effort_nested_dict_level_injects(self):
        result = run_hook(
            {"session_id": "sess-nested-001", "prompt": "x",
             "effort": {"level": "xhigh", "tokens": 8000}},
            marker_dir=self._marker_dir,
        )
        self.assertTrue(_is_injection(result), "nested effort.level=xhigh should inject")

    def test_effort_nested_dict_low_returns_empty(self):
        result = run_hook(
            {"session_id": "sess-nested-002", "prompt": "x",
             "effort": {"level": "low"}},
            marker_dir=self._marker_dir,
        )
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # (d/e) Effort from env vars
    # ------------------------------------------------------------------

    def test_effort_from_claude_effort_env(self):
        result = run_hook(
            {"session_id": "sess-env-claude", "prompt": "x"},
            env_extra={"CLAUDE_EFFORT": "max"},
            marker_dir=self._marker_dir,
        )
        self.assertTrue(_is_injection(result), "CLAUDE_EFFORT=max should inject")

    def test_effort_from_unifable_effort_env(self):
        result = run_hook(
            {"session_id": "sess-env-unifable", "prompt": "x"},
            env_extra={"UNIFABLE_EFFORT": "ultracode"},
            marker_dir=self._marker_dir,
        )
        self.assertTrue(_is_injection(result), "UNIFABLE_EFFORT=ultracode should inject")

    def test_env_effort_dedup_same_session(self):
        payload = {"session_id": "sess-env-dedup", "prompt": "x"}
        run_hook(payload, env_extra={"CLAUDE_EFFORT": "xhigh"}, marker_dir=self._marker_dir)
        result = run_hook(payload, env_extra={"CLAUDE_EFFORT": "xhigh"}, marker_dir=self._marker_dir)
        self.assertEqual(result, {}, "second call with same session should be deduped")

    # ------------------------------------------------------------------
    # (f) Fail open on bad input
    # ------------------------------------------------------------------

    def test_empty_stdin_returns_empty(self):
        env = dict(os.environ)
        env["UNIFABLE_MARKER_DIR"] = self._marker_dir
        env.pop("CLAUDE_EFFORT", None)
        env.pop("UNIFABLE_EFFORT", None)
        p = subprocess.run(
            [PY, HOOK],
            input="",
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(p.returncode, 0)
        out = p.stdout.strip()
        result = json.loads(out) if out else {}
        self.assertEqual(result, {})

    def test_malformed_stdin_returns_empty(self):
        env = dict(os.environ)
        env["UNIFABLE_MARKER_DIR"] = self._marker_dir
        env.pop("CLAUDE_EFFORT", None)
        env.pop("UNIFABLE_EFFORT", None)
        p = subprocess.run(
            [PY, HOOK],
            input="{not valid json",
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(p.returncode, 0)
        out = p.stdout.strip()
        result = json.loads(out) if out else {}
        self.assertEqual(result, {})

    # ------------------------------------------------------------------
    # (g) Content sanity check
    # ------------------------------------------------------------------

    def test_injected_context_contains_unifable_content(self):
        result = run_hook(
            {"session_id": "sess-content-001", "prompt": "x", "effort": "xhigh"},
            marker_dir=self._marker_dir,
        )
        context = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        self.assertIn("unifable", context.lower(),
                      "injected context should mention unifable")


class TestPlaybookDedup(unittest.TestCase):
    """Tests for playbook paragraph suppression when router packs already fired."""

    def test_no_tags_includes_all_paragraphs(self):
        import importlib.util
        hook_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
        gate_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "gate")
        for p in (gate_dir, hook_dir):
            if p not in sys.path:
                sys.path.insert(0, p)
        import gate_prompt_effort
        ctx = gate_prompt_effort._playbook_context()
        self.assertIn("Investigation:", ctx)
        self.assertIn("Verification grounding:", ctx)

    def test_investigation_tag_suppresses_investigation_paragraph(self):
        import importlib.util
        hook_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
        gate_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "gate")
        for p in (gate_dir, hook_dir):
            if p not in sys.path:
                sys.path.insert(0, p)
        import gate_prompt_effort
        ctx = gate_prompt_effort._playbook_context({"investigation"})
        self.assertNotIn("Investigation: reproduce first", ctx)
        self.assertIn("Verification grounding:", ctx)

    def test_grounding_tag_suppresses_grounding_paragraph(self):
        import importlib.util
        hook_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
        gate_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "gate")
        for p in (gate_dir, hook_dir):
            if p not in sys.path:
                sys.path.insert(0, p)
        import gate_prompt_effort
        ctx = gate_prompt_effort._playbook_context({"grounding"})
        self.assertIn("Investigation: reproduce first", ctx)
        self.assertNotIn("Verification grounding:", ctx)

    def test_both_tags_suppress_both_paragraphs(self):
        import importlib.util
        hook_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
        gate_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "gate")
        for p in (gate_dir, hook_dir):
            if p not in sys.path:
                sys.path.insert(0, p)
        import gate_prompt_effort
        ctx = gate_prompt_effort._playbook_context({"investigation", "grounding"})
        self.assertNotIn("Investigation: reproduce first", ctx)
        self.assertNotIn("Verification grounding:", ctx)
        self.assertIn("Working style:", ctx)
        self.assertIn("Escalation:", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
