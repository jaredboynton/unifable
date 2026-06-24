#!/usr/bin/env python3
"""Tests for scripts/gate/plugin_root.py — stale cache fallback."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

from plugin_root import resolve_plugin_root  # noqa: E402


def _write_hooks(root: Path) -> None:
    hooks = root / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre_tool_use.py").write_text("# stub\n", encoding="utf-8")


class TestResolvePluginRoot(unittest.TestCase):
    def test_explicit_valid_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "plugin"
            root.mkdir()
            _write_hooks(root)
            self.assertEqual(resolve_plugin_root(root), root.resolve())

    def test_explicit_missing_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "plugin"
            root.mkdir()
            self.assertIsNone(resolve_plugin_root(root))

    def test_env_root_used_when_hooks_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "1.9.60"
            root.mkdir()
            _write_hooks(root)
            with mock.patch.dict(os.environ, {"PLUGIN_ROOT": str(root)}, clear=False):
                self.assertEqual(resolve_plugin_root(), root.resolve())

    def test_stale_env_falls_back_to_latest_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "codex" / "plugins" / "cache" / "unifable" / "unifable"
            old = cache / "1.9.59"
            new = cache / "1.9.60"
            _write_hooks(new)
            stale = Path(td) / "missing" / "1.9.59"
            with mock.patch.dict(os.environ, {"PLUGIN_ROOT": str(stale)}, clear=False):
                with mock.patch("plugin_root._CACHE_ROOTS", (cache,)):
                    resolved = resolve_plugin_root()
            self.assertEqual(resolved, new.resolve())

    def test_missing_everything_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "empty" / "unifable" / "unifable"
            cache.mkdir(parents=True)
            with mock.patch.dict(os.environ, {}, clear=True):
                os.environ.pop("PLUGIN_ROOT", None)
                os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
                os.environ.pop("UNIFABLE_PLUGIN_ROOT", None)
                with mock.patch("plugin_root._CACHE_ROOTS", (cache,)):
                    self.assertIsNone(resolve_plugin_root())


if __name__ == "__main__":
    unittest.main()
