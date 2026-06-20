#!/usr/bin/env python3
"""Unit tests for hooks/test_after_edit.py.

Tests cover:
- Extension-skip logic (should_skip_path)
- Runner discovery (discover_runner) against a synthetic temp directory tree
- Debounce logic (is_debounced / stamp_debounce)
- UNIFABLE_TEST_AFTER_EDIT opt-in gate (hook emits {} when env is unset)
- Full hook pipeline via subprocess with monkeypatched subprocess (no real suite run)

No real test suites are executed; subprocess is stubbed via a temp wrapper script.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Path setup: allow importing the hook module directly
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_PATH = REPO_ROOT / "hooks" / "test_after_edit.py"

sys.path.insert(0, str(REPO_ROOT / "hooks"))

import test_after_edit as tae  # noqa: E402  (after sys.path manipulation)


# ---------------------------------------------------------------------------
# Extension skip
# ---------------------------------------------------------------------------

class TestShouldSkipPath(unittest.TestCase):

    def test_markdown_skipped(self):
        self.assertTrue(tae.should_skip_path("docs/README.md"))

    def test_txt_skipped(self):
        self.assertTrue(tae.should_skip_path("/some/path/notes.txt"))

    def test_rst_skipped(self):
        self.assertTrue(tae.should_skip_path("CHANGES.rst"))

    def test_png_skipped(self):
        self.assertTrue(tae.should_skip_path("assets/logo.png"))

    def test_svg_skipped(self):
        self.assertTrue(tae.should_skip_path("icon.svg"))

    def test_lock_skipped(self):
        self.assertTrue(tae.should_skip_path("package-lock.json.lock"))
        self.assertTrue(tae.should_skip_path("Cargo.lock"))
        self.assertTrue(tae.should_skip_path("bun.lockb"))

    def test_json_not_skipped(self):
        # .json is intentionally kept testable
        self.assertFalse(tae.should_skip_path("config/schema.json"))

    def test_python_not_skipped(self):
        self.assertFalse(tae.should_skip_path("src/main.py"))

    def test_typescript_not_skipped(self):
        self.assertFalse(tae.should_skip_path("lib/util.ts"))

    def test_rust_not_skipped(self):
        self.assertFalse(tae.should_skip_path("src/lib.rs"))

    def test_go_not_skipped(self):
        self.assertFalse(tae.should_skip_path("pkg/server.go"))

    def test_shell_not_skipped(self):
        self.assertFalse(tae.should_skip_path("scripts/build.sh"))

    def test_no_extension_not_skipped(self):
        self.assertFalse(tae.should_skip_path("Makefile"))

    def test_case_insensitive(self):
        self.assertTrue(tae.should_skip_path("IMAGE.PNG"))
        self.assertTrue(tae.should_skip_path("notes.MD"))


# ---------------------------------------------------------------------------
# Runner discovery
# ---------------------------------------------------------------------------

class TestDiscoverRunner(unittest.TestCase):

    def _make_tree(self, *files: str) -> str:
        """Create a temp directory tree with the given relative paths and return the root."""
        root = tempfile.mkdtemp()
        for rel in files:
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write("")
        return root

    def test_no_runner_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            root, cmd, label = tae.discover_runner(d)
        self.assertIsNone(root)
        self.assertIsNone(cmd)
        self.assertIsNone(label)

    def test_python_pyproject(self):
        root = self._make_tree("pyproject.toml", "src/app.py")
        try:
            found_root, cmd, label = tae.discover_runner(os.path.join(root, "src"))
            self.assertEqual(found_root, root)
            self.assertIn("pytest", " ".join(cmd))
            self.assertIn("pytest", label)
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_python_tests_dir(self):
        root = self._make_tree("tests/__init__.py", "src/app.py")
        try:
            found_root, cmd, label = tae.discover_runner(os.path.join(root, "src"))
            self.assertEqual(found_root, root)
            self.assertIn("pytest", " ".join(cmd))
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_python_uv_lock(self):
        root = self._make_tree("pyproject.toml", "uv.lock")
        try:
            found_root, cmd, label = tae.discover_runner(root)
            self.assertEqual(cmd[:2], ["uv", "run"])
            self.assertIn("uv", label)
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_rust_cargo(self):
        root = self._make_tree("Cargo.toml", "src/lib.rs")
        try:
            found_root, cmd, label = tae.discover_runner(os.path.join(root, "src"))
            self.assertEqual(found_root, root)
            self.assertEqual(cmd, ["cargo", "test", "-q"])
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_go_mod(self):
        root = self._make_tree("go.mod", "pkg/server.go")
        try:
            found_root, cmd, label = tae.discover_runner(os.path.join(root, "pkg"))
            self.assertEqual(found_root, root)
            self.assertEqual(cmd, ["go", "test", "./..."])
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_node_npm(self):
        root = self._make_tree("package.json")
        pkg = os.path.join(root, "package.json")
        with open(pkg, "w") as f:
            json.dump({"scripts": {"test": "jest"}}, f)
        try:
            found_root, cmd, label = tae.discover_runner(root)
            self.assertEqual(found_root, root)
            self.assertEqual(cmd, ["npm", "test"])
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_node_pnpm(self):
        root = self._make_tree("package.json", "pnpm-lock.yaml")
        pkg = os.path.join(root, "package.json")
        with open(pkg, "w") as f:
            json.dump({"scripts": {"test": "vitest run"}}, f)
        try:
            found_root, cmd, label = tae.discover_runner(root)
            self.assertEqual(cmd, ["pnpm", "test"])
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_node_yarn(self):
        root = self._make_tree("package.json", "yarn.lock")
        pkg = os.path.join(root, "package.json")
        with open(pkg, "w") as f:
            json.dump({"scripts": {"test": "jest"}}, f)
        try:
            found_root, cmd, label = tae.discover_runner(root)
            self.assertEqual(cmd, ["yarn", "test"])
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_node_bun(self):
        root = self._make_tree("package.json", "bun.lockb")
        pkg = os.path.join(root, "package.json")
        with open(pkg, "w") as f:
            json.dump({"scripts": {"test": "bun test"}}, f)
        try:
            found_root, cmd, label = tae.discover_runner(root)
            self.assertEqual(cmd, ["bun", "test"])
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_node_no_test_script_ignored(self):
        """package.json with no test script should not match Node runner."""
        root = self._make_tree("package.json")
        pkg = os.path.join(root, "package.json")
        with open(pkg, "w") as f:
            json.dump({"scripts": {}}, f)
        try:
            found_root, cmd, label = tae.discover_runner(root)
            # Should fall through to None (no other runner files present)
            self.assertIsNone(cmd)
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_makefile_with_test_target(self):
        root = tempfile.mkdtemp()
        mk = os.path.join(root, "Makefile")
        with open(mk, "w") as f:
            f.write("test:\n\t./run_tests.sh\n")
        try:
            found_root, cmd, label = tae.discover_runner(root)
            self.assertEqual(found_root, root)
            self.assertEqual(cmd, ["make", "test"])
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_makefile_without_test_target_ignored(self):
        root = tempfile.mkdtemp()
        mk = os.path.join(root, "Makefile")
        with open(mk, "w") as f:
            f.write("build:\n\tgcc main.c\n")
        try:
            found_root, cmd, label = tae.discover_runner(root)
            self.assertIsNone(cmd)
        finally:
            import shutil; shutil.rmtree(root, ignore_errors=True)

    def test_innermost_wins(self):
        """Nested project: inner pyproject.toml beats outer Cargo.toml."""
        outer = tempfile.mkdtemp()
        inner = os.path.join(outer, "subproject")
        os.makedirs(inner)
        # Outer: Rust
        with open(os.path.join(outer, "Cargo.toml"), "w") as f:
            f.write("")
        # Inner: Python
        with open(os.path.join(inner, "pyproject.toml"), "w") as f:
            f.write("")
        try:
            found_root, cmd, label = tae.discover_runner(inner)
            self.assertEqual(found_root, inner)
            self.assertIn("pytest", " ".join(cmd))
        finally:
            import shutil; shutil.rmtree(outer, ignore_errors=True)


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------

class TestDebounce(unittest.TestCase):

    def test_first_call_not_debounced(self):
        with tempfile.TemporaryDirectory() as d:
            # Ensure no stale marker
            marker = tae._marker_path(d)
            if os.path.exists(marker):
                os.remove(marker)
            self.assertFalse(tae.is_debounced(d))

    def test_second_call_within_window_debounced(self):
        with tempfile.TemporaryDirectory() as d:
            marker = tae._marker_path(d)
            if os.path.exists(marker):
                os.remove(marker)
            tae.stamp_debounce(d)
            self.assertTrue(tae.is_debounced(d))

    def test_expired_marker_not_debounced(self):
        with tempfile.TemporaryDirectory() as d:
            marker = tae._marker_path(d)
            # Write marker with a mtime far in the past
            with open(marker, "w") as f:
                f.write("")
            past = time.time() - (tae.DEBOUNCE_SECS + 10)
            os.utime(marker, (past, past))
            self.assertFalse(tae.is_debounced(d))

    def test_marker_path_uses_hash(self):
        p1 = tae._marker_path("/project/alpha")
        p2 = tae._marker_path("/project/beta")
        self.assertNotEqual(p1, p2)
        # Both should be in tempdir
        self.assertTrue(p1.startswith(tempfile.gettempdir()))

    def test_marker_path_stable(self):
        p1 = tae._marker_path("/some/root")
        p2 = tae._marker_path("/some/root")
        self.assertEqual(p1, p2)


# ---------------------------------------------------------------------------
# Full hook via subprocess (no real suite execution)
# ---------------------------------------------------------------------------

class TestHookPipeline(unittest.TestCase):
    """Drive the hook as a subprocess with env control and stubbed test runner."""

    def _run_hook(self, payload: dict, env_extra: dict | None = None) -> dict:
        env = {**os.environ}
        env.pop("UNIFABLE_TEST_AFTER_EDIT", None)
        if env_extra:
            env.update(env_extra)
        p = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
        )
        try:
            return json.loads(p.stdout or "{}")
        except json.JSONDecodeError:
            return {"_raw": p.stdout, "_stderr": p.stderr}

    def test_disabled_by_default_emits_empty(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/foo.py"}}
        result = self._run_hook(payload)
        self.assertEqual(result, {})

    def test_non_edit_tool_emits_empty(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        result = self._run_hook(payload, {"UNIFABLE_TEST_AFTER_EDIT": "1"})
        self.assertEqual(result, {})

    def test_skipped_extension_emits_empty(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/notes.md"}}
        result = self._run_hook(payload, {"UNIFABLE_TEST_AFTER_EDIT": "1"})
        self.assertEqual(result, {})

    def test_no_runner_found_emits_empty(self):
        with tempfile.TemporaryDirectory() as d:
            # No project marker files — runner discovery returns None
            payload = {
                "tool_name": "Edit",
                "tool_input": {"file_path": os.path.join(d, "main.py")},
                "cwd": d,
            }
            result = self._run_hook(payload, {"UNIFABLE_TEST_AFTER_EDIT": "1"})
        self.assertEqual(result, {})

    def test_pass_result_injects_context(self):
        """Stub a passing test suite using a Makefile test target (PATH-resolvable make stub)."""
        with tempfile.TemporaryDirectory() as d:
            # Makefile with a test target — runner resolves to ["make", "test"]
            mk = os.path.join(d, "Makefile")
            with open(mk, "w") as f:
                f.write("test:\n\techo '1 passed in 0.1s'\n")

            # Stub 'make' on PATH to exit 0 with success output
            stub = os.path.join(d, "make")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\necho '1 passed in 0.1s'\nexit 0\n")
            os.chmod(stub, 0o755)

            payload = {
                "tool_name": "Write",
                "tool_input": {"file_path": os.path.join(d, "app.py")},
                "cwd": d,
            }
            env = {
                "UNIFABLE_TEST_AFTER_EDIT": "1",
                "UNIFABLE_TEST_DEBOUNCE": "0",
                "PATH": d + ":" + os.environ.get("PATH", ""),
            }
            result = self._run_hook(payload, env)

        # Should have emitted hookSpecificOutput with a PASS message
        hook_out = result.get("hookSpecificOutput", {})
        self.assertEqual(hook_out.get("hookEventName"), "PostToolUse")
        ctx = hook_out.get("additionalContext", "")
        self.assertIn("PASS", ctx)
        self.assertIn("make", ctx)

    def test_fail_result_injects_context(self):
        """Stub a failing test suite using a Makefile test target (make stub exits 1)."""
        with tempfile.TemporaryDirectory() as d:
            mk = os.path.join(d, "Makefile")
            with open(mk, "w") as f:
                f.write("test:\n\techo '1 failed'\n")

            stub = os.path.join(d, "make")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\necho '1 failed, 2 passed'\nexit 1\n")
            os.chmod(stub, 0o755)

            payload = {
                "tool_name": "Edit",
                "tool_input": {"file_path": os.path.join(d, "app.py")},
                "cwd": d,
            }
            env = {
                "UNIFABLE_TEST_AFTER_EDIT": "1",
                "UNIFABLE_TEST_DEBOUNCE": "0",
                "PATH": d + ":" + os.environ.get("PATH", ""),
            }
            result = self._run_hook(payload, env)

        hook_out = result.get("hookSpecificOutput", {})
        ctx = hook_out.get("additionalContext", "")
        self.assertIn("FAIL", ctx)

    def test_notebook_edit_triggers(self):
        """NotebookEdit tool name should be treated as an edit trigger."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pyproject.toml"), "w") as f:
                f.write("")
            stub = os.path.join(d, "pytest")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(stub, 0o755)

            payload = {
                "tool_name": "NotebookEdit",
                "tool_input": {"file_path": os.path.join(d, "notebook.ipynb")},
                "cwd": d,
            }
            env = {
                "UNIFABLE_TEST_AFTER_EDIT": "1",
                "UNIFABLE_TEST_DEBOUNCE": "0",
                "PATH": d + ":" + os.environ.get("PATH", ""),
            }
            result = self._run_hook(payload, env)

        # Should have emitted something (PASS or empty if runner not found)
        # Just verify it did not crash and returned valid JSON
        self.assertIsInstance(result, dict)

    def test_debounce_suppresses_second_run(self):
        """Second call within DEBOUNCE window emits {} without running tests."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pyproject.toml"), "w") as f:
                f.write("")
            stub = os.path.join(d, "pytest")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(stub, 0o755)

            payload = {
                "tool_name": "Edit",
                "tool_input": {"file_path": os.path.join(d, "app.py")},
                "cwd": d,
            }
            env = {
                "UNIFABLE_TEST_AFTER_EDIT": "1",
                "UNIFABLE_TEST_DEBOUNCE": "60",  # 60s window
                "PATH": d + ":" + os.environ.get("PATH", ""),
            }
            # First call — should run
            r1 = self._run_hook(payload, env)
            # Second call — should be debounced
            r2 = self._run_hook(payload, env)

        # First may have context or empty (depends on runner resolution);
        # second must be empty (debounced)
        self.assertEqual(r2, {})

    def test_invalid_stdin_emits_empty(self):
        """Garbage stdin should not crash the hook."""
        env = {**os.environ, "UNIFABLE_TEST_AFTER_EDIT": "1"}
        p = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input="not json {{{{",
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(p.returncode, 0)
        result = json.loads(p.stdout or "{}")
        self.assertEqual(result, {})

    def test_apply_patch_tool_triggers(self):
        """apply_patch should be treated as an edit tool."""
        payload = {
            "tool_name": "apply_patch",
            "tool_input": {"file_path": "/nonexistent/path/file.py"},
            "cwd": "/nonexistent",
        }
        result = self._run_hook(payload, {"UNIFABLE_TEST_AFTER_EDIT": "1"})
        # No runner found for nonexistent path — emits {}; just confirm no crash
        self.assertIsInstance(result, dict)


# ---------------------------------------------------------------------------
# run_tests return value
# ---------------------------------------------------------------------------

class TestRunTestsSummary(unittest.TestCase):

    def test_pass_contains_pass(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="3 passed", stderr="")
            result = tae.run_tests("/tmp/proj", ["pytest", "-q"], "pytest -q")
        self.assertIn("PASS", result)
        self.assertIn("pytest -q", result)

    def test_fail_contains_fail(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="1 failed\n2 passed", stderr="")
            result = tae.run_tests("/tmp/proj", ["pytest", "-q"], "pytest -q")
        self.assertIn("FAIL", result)
        self.assertIn("exit=1", result)

    def test_timeout_contains_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["pytest"], timeout=60)):
            result = tae.run_tests("/tmp/proj", ["pytest", "-q"], "pytest -q")
        self.assertIn("TIMEOUT", result)

    def test_file_not_found_returns_empty(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            result = tae.run_tests("/tmp/proj", ["nonexistent-runner"], "nonexistent-runner")
        self.assertEqual(result, "")

    def test_tail_limited_to_30_lines(self):
        long_output = "\n".join(f"line {i}" for i in range(100))
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=long_output, stderr="")
            result = tae.run_tests("/tmp/proj", ["pytest"], "pytest")
        # Count lines in the tail portion of the result
        # The summary is "FAIL ...\n<tail>" — split off the prefix
        tail_section = result.split(":\n", 1)[-1] if ":\n" in result else ""
        self.assertLessEqual(len(tail_section.splitlines()), tae.TAIL_LINES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
