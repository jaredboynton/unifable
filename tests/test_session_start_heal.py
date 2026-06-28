#!/usr/bin/env python3
"""Regression test: SessionStart hook heals ~/.unifable/current when stale.

When the loaded plugin root is newer than ~/.unifable/current and the cache scan
cannot advance it (empty cache), the SessionStart hook must re-seed current from
the effective plugin root via cli_install.ensure_cli so the global launchers
(unitrace/unisearch/unifusion/...) resolve on PATH. Fails before the ensure_cli
wiring landed in hooks/session_start.py; passes after.

Run: python3 -m pytest tests/test_session_start_heal.py -q
"""

from __future__ import annotations

import importlib
import io
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = str(REPO / "scripts" / "gate")
HOOKS = str(REPO / "hooks")

sys.path.insert(0, GATE)

import runtime_sync  # noqa: E402
from runtime_sync import current_version  # noqa: E402

# Full runtime tree (with skills) for the "loaded" plugin root the heal seeds
# from; minimal tree (no skills) for the stale old version, so the absence of
# skills/unitrace/scripts/unitrace.sh mirrors the real 1.18.0 stranded state.
_FULL_RUNTIME_DIRS = ("hooks", "scripts", "unifable_runtime", "bin", "setup", "packs", "skills")
_MINIMAL_RUNTIME_DIRS = ("hooks", "unifable_runtime")


def _seed_version(dest: Path, version: str, dirs: tuple[str, ...]) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    for name in dirs:
        src = REPO / name
        if src.is_dir():
            shutil.copytree(src, dest / name, dirs_exist_ok=True)
    (dest / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (dest / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "unifable", "version": version}), encoding="utf-8"
    )
    return dest


def _load_session_start():
    sys.path.insert(0, HOOKS)
    return importlib.import_module("session_start")


def test_session_start_heals_stale_current(tmp_path, monkeypatch):
    home = tmp_path / "home"
    bindir = tmp_path / "bindir"
    cache = tmp_path / "cache"  # intentionally empty: cache-scan sync cannot advance
    devtree = tmp_path / "devtree" / "1.9.99"  # the "loaded" plugin root (newer)
    old_vdir = tmp_path / "old" / "1.9.10"  # the stale version current points at
    bindir.mkdir()
    cache.mkdir()

    _seed_version(old_vdir, "1.9.10", _MINIMAL_RUNTIME_DIRS)
    _seed_version(devtree, "1.9.99", _FULL_RUNTIME_DIRS)

    for key in ("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "UNIFABLE_PLUGIN_ROOT"):
        monkeypatch.setenv(key, str(devtree))
    monkeypatch.setenv("UNIFABLE_HOME", str(home))
    monkeypatch.setenv("UNIFABLE_BIN_DIR", str(bindir))
    monkeypatch.setenv("UNIFABLE_CACHE_ROOTS", str(cache))
    monkeypatch.setenv("UNIFABLE_JANITOR", "0")
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path / "data"))

    # Pre-seed a stale current -> 1.9.10 (no skills tree, so no unitrace.sh).
    runtime_sync.sync_runtime(source=str(old_vdir))
    assert current_version(home) == "1.9.10"
    assert not (home / "current" / "skills" / "unitrace" / "scripts" / "unitrace.sh").is_file()

    session_start = _load_session_start()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path)})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    rc = session_start.main()

    assert rc == 0
    # The heal must flip current to the loaded plugin version...
    assert current_version(home) == "1.9.99", "SessionStart must heal stale ~/.unifable/current"
    # ...and seed the skill scripts so the global launchers resolve.
    assert (home / "current" / "skills" / "unitrace" / "scripts" / "unitrace.sh").is_file()
    # The unitrace bootstrap launcher must be linked into the bindir.
    assert (bindir / "unitrace").is_symlink()
