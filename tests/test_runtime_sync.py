#!/usr/bin/env python3
"""Regression: the ~/.unifable runtime must survive cache-version deletion.

Reproduces the exit-127 upgrade bug. The plugin cache dir for a version is
deleted on marketplace upgrade; nothing on the runtime path may point into it.
After `sync_runtime()` seeds ~/.unifable from a cache version, deleting that
cache dir must NOT break `~/.local/bin/unifable-hook` (it execs from the stable
~/.unifable/current copy, not the cache).

Run: python3 -m pytest tests/test_runtime_sync.py -q
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import runtime_sync  # noqa: E402

# Top-level dirs a fake cache "version" needs to be a runnable plugin.
# Includes unifable_runtime so the seeded runtime can satisfy the shared
# interpreter preflight the bootstraps run, and skills so the unifusion
# launcher bootstrap can resolve the synced panel script.
_RUNTIME_DIRS = ("hooks", "scripts", "unifable_runtime", "bin", "setup", "packs", "skills")


def _seed_cache_version(cache_parent: Path, version: str) -> Path:
    """Create a fake cache version dir that is a real, runnable copy of the plugin."""
    vdir = cache_parent / version
    vdir.mkdir(parents=True, exist_ok=True)
    for name in _RUNTIME_DIRS:
        src = REPO / name
        if src.is_dir():
            shutil.copytree(src, vdir / name, dirs_exist_ok=True)
    assert (vdir / "hooks" / "pre_tool_use.py").is_file()
    return vdir


def _env(home: Path, bdir: Path, cache_parent: Path) -> dict:
    env = dict(os.environ)
    env["UNIFABLE_HOME"] = str(home)
    env["UNIFABLE_BIN_DIR"] = str(bdir)
    env["UNIFABLE_CACHE_ROOTS"] = str(cache_parent)
    env["HOME"] = str(home.parent)  # keep stray ~ lookups inside the sandbox
    return env


def _apply_env(monkeypatch, env: dict) -> None:
    for key in ("UNIFABLE_HOME", "UNIFABLE_BIN_DIR", "UNIFABLE_CACHE_ROOTS"):
        monkeypatch.setenv(key, env[key])


def _run_hook(hook_bin: Path, env: dict) -> tuple[int, str]:
    payload = json.dumps({"prompt": "hello", "session_id": "rt-sync-test", "cwd": str(REPO)})
    proc = subprocess.run(
        [str(hook_bin), "router.sh"],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout


def _is_dict_or_empty(out: str) -> bool:
    trimmed = out.strip()
    if not trimmed:
        return True
    try:
        return isinstance(json.loads(trimmed), dict)
    except json.JSONDecodeError:
        # router.sh may emit plain additionalContext text; only braces-that-don't-parse is a fail.
        return not trimmed.startswith(("{", "["))


def test_sync_seeds_runtime_and_links(tmp_path, monkeypatch):
    home = tmp_path / "dot-unifable"
    bdir = tmp_path / "local-bin"
    cache = tmp_path / "cache"
    env = _env(home, bdir, cache)
    _apply_env(monkeypatch, env)

    _seed_cache_version(cache, "1.0.0")

    assert runtime_sync.sync_runtime() is True
    assert runtime_sync.current_version(home) == "1.0.0"

    current = home / "current"
    assert current.is_symlink()
    assert current.resolve() == (home / "versions" / "1.0.0").resolve()

    hook_link = bdir / "unifable-hook"
    assert hook_link.is_symlink()
    assert hook_link.resolve() == (home / "bin" / "unifable-hook").resolve()
    assert (home / "bin" / "unifable-hook").is_file()  # real bootstrap, not a cache symlink


def test_sync_installs_unifusion_launcher(tmp_path, monkeypatch):
    # The unifusion launcher rides the same bootstrap path as the unifable CLIs:
    # a real file under ~/.unifable/bin, symlinked from ~/.local/bin, that execs
    # the synced skill script under current/skills/unifusion.
    home = tmp_path / "dot-unifable"
    bdir = tmp_path / "local-bin"
    cache = tmp_path / "cache"
    env = _env(home, bdir, cache)
    _apply_env(monkeypatch, env)

    _seed_cache_version(cache, "1.0.0")
    assert runtime_sync.sync_runtime() is True

    link = bdir / "unifusion"
    assert link.is_symlink()
    assert link.resolve() == (home / "bin" / "unifusion").resolve()
    bootstrap = home / "bin" / "unifusion"
    assert bootstrap.is_file()  # real bootstrap, not a cache symlink
    assert "skills/unifusion/scripts/unifusion.sh" in bootstrap.read_text()

    # End-to-end resolution: the bootstrap must exec the synced skill script.
    # Stub the synced script so we exercise only the launcher wiring, not the
    # real panel fan-out (which would call paid model CLIs).
    synced = home / "current" / "skills" / "unifusion" / "scripts" / "unifusion.sh"
    synced.parent.mkdir(parents=True, exist_ok=True)
    synced.write_text("#!/usr/bin/env bash\necho \"unifusion-stub: $1\"\n")
    synced.chmod(0o755)

    q = tmp_path / "q.md"
    q.write_text("hello panel\n")
    proc = subprocess.run(
        [str(link), str(q)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"unifusion-stub: {q}"


def test_runtime_survives_cache_deletion(tmp_path, monkeypatch):
    home = tmp_path / "dot-unifable"
    bdir = tmp_path / "local-bin"
    cache = tmp_path / "cache"
    env = _env(home, bdir, cache)
    _apply_env(monkeypatch, env)

    _seed_cache_version(cache, "1.0.0")
    assert runtime_sync.sync_runtime() is True

    # Marketplace upgrade deletes the old cache version dir.
    shutil.rmtree(cache / "1.0.0")
    assert not (cache / "1.0.0").exists()

    rc, out = _run_hook(bdir / "unifable-hook", env)
    assert rc == 0, f"hook exited {rc} after cache deletion (the 127 bug); output: {out!r}"
    assert _is_dict_or_empty(out), f"hook emitted malformed output: {out!r}"


def test_sync_flips_to_newer_version(tmp_path, monkeypatch):
    home = tmp_path / "dot-unifable"
    bdir = tmp_path / "local-bin"
    cache = tmp_path / "cache"
    env = _env(home, bdir, cache)
    _apply_env(monkeypatch, env)

    _seed_cache_version(cache, "1.0.0")
    assert runtime_sync.sync_runtime() is True
    assert runtime_sync.current_version(home) == "1.0.0"

    # New version lands in the cache.
    _seed_cache_version(cache, "1.0.1")
    assert runtime_sync.sync_runtime() is True
    assert runtime_sync.current_version(home) == "1.0.1"

    # Old cache dir can now vanish; runtime keeps working on the new version.
    shutil.rmtree(cache / "1.0.0")
    rc, out = _run_hook(bdir / "unifable-hook", env)
    assert rc == 0, f"hook exited {rc}; output: {out!r}"


def test_sync_noop_when_current_is_latest(tmp_path, monkeypatch):
    home = tmp_path / "dot-unifable"
    bdir = tmp_path / "local-bin"
    cache = tmp_path / "cache"
    env = _env(home, bdir, cache)
    _apply_env(monkeypatch, env)

    _seed_cache_version(cache, "1.0.0")
    assert runtime_sync.sync_runtime() is True
    assert runtime_sync.sync_runtime() is False  # already current -> no flip


def test_sync_from_explicit_source(tmp_path, monkeypatch):
    home = tmp_path / "dot-unifable"
    bdir = tmp_path / "local-bin"
    cache = tmp_path / "cache"
    env = _env(home, bdir, cache)
    _apply_env(monkeypatch, env)

    # Two versions cached; an explicit older source must win over the latest-cache scan.
    _seed_cache_version(cache, "2.0.0")
    older = _seed_cache_version(cache, "1.5.0")

    assert runtime_sync.sync_runtime(source=str(older)) is True
    assert runtime_sync.current_version(home) == "1.5.0"


def test_sync_copies_unifable_runtime_into_current(tmp_path, monkeypatch):
    # Wave 2 contract: the shared package lands in the synced runtime so hook
    # and CLI launchers can import unifable_runtime from ~/.unifable/current.
    home = tmp_path / "dot-unifable"
    bdir = tmp_path / "local-bin"
    cache = tmp_path / "cache"
    env = _env(home, bdir, cache)
    _apply_env(monkeypatch, env)

    _seed_cache_version(cache, "1.0.0")
    assert runtime_sync.sync_runtime() is True

    pkg = home / "current" / "unifable_runtime" / "__init__.py"
    assert pkg.is_file(), "unifable_runtime not synced into ~/.unifable/current"

    # The synced package is importable from the runtime root and enforces 3.12+.
    root = (home / "current").resolve()
    proc = subprocess.run(
        [sys.executable, "-c", "import unifable_runtime as rt; print(rt.MIN_PYTHON)"],
        env={**env, "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "(3, 12)" in proc.stdout


def test_synced_hook_runs_with_preflight(tmp_path, monkeypatch):
    # The bootstrap now runs the 3.12+ preflight before dispatch; on a supported
    # interpreter (this one) the hook must still succeed and emit dict-or-empty.
    home = tmp_path / "dot-unifable"
    bdir = tmp_path / "local-bin"
    cache = tmp_path / "cache"
    env = _env(home, bdir, cache)
    _apply_env(monkeypatch, env)

    _seed_cache_version(cache, "1.0.0")
    assert runtime_sync.sync_runtime() is True

    rc, out = _run_hook(bdir / "unifable-hook", env)
    assert rc == 0, f"hook exited {rc} with preflight present; output: {out!r}"
    assert _is_dict_or_empty(out), f"hook emitted malformed output: {out!r}"


def test_invalid_source_falls_back_to_latest_cache(tmp_path, monkeypatch):
    home = tmp_path / "dot-unifable"
    bdir = tmp_path / "local-bin"
    cache = tmp_path / "cache"
    env = _env(home, bdir, cache)
    _apply_env(monkeypatch, env)

    _seed_cache_version(cache, "1.0.0")
    # A non-semver source (e.g. a dev repo root) must not no-op; fall back to latest cache.
    assert runtime_sync.sync_runtime(source=str(tmp_path / "not-a-version")) is True
    assert runtime_sync.current_version(home) == "1.0.0"


def test_fail_open_when_no_cache(tmp_path, monkeypatch):
    home = tmp_path / "dot-unifable"
    bdir = tmp_path / "local-bin"
    cache = tmp_path / "cache"  # never created
    env = _env(home, bdir, cache)
    _apply_env(monkeypatch, env)

    assert runtime_sync.sync_runtime() is False
    assert runtime_sync.current_version(home) is None
