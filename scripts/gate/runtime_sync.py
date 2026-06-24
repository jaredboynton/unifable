#!/usr/bin/env python3
"""Maintain a stable unifable runtime under ~/.unifable, decoupled from the plugin cache.

The host marketplace deletes old versioned cache dirs on upgrade. Anything the hooks
exec straight from the cache (`~/.local/bin/unifable-hook` symlinked into a versioned
cache dir) therefore dangles and the OS returns exit 127 before the dispatcher's own
resolver can run. This module keeps the running code OUT of the cache:

  ~/.unifable/versions/<v>/   self-contained copy of one plugin version's runtime
  ~/.unifable/current         -> versions/<v>   (atomic symlink, flipped after validation)
  ~/.unifable/bin/unifable*   stable bootstrap launchers (real files) that exec from current
  ~/.local/bin/unifable*      -> ~/.unifable/bin/unifable*   (~/.unifable is never deleted)

The cache is only a download source. Nothing on the runtime path points into it, so
deleting any cache version cannot brick a session. `sync_runtime()` is invoked from the
SessionStart hook (and as a UserPromptSubmit backstop); it copies a newer cache version
into ~/.unifable and atomically flips `current` at the next session start.

Fail open: any error leaves the existing runtime untouched and returns False. A sync that
hard-locks a session on its own bug is worse than no sync (see AGENTS.md fail-open rule).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from cli_install import parse_version
except ImportError:  # pragma: no cover
    from scripts.gate.cli_install import parse_version

# Top-level entries copied from a cache version into ~/.unifable/versions/<v>.
_RUNTIME_TREE = (
    "hooks",
    "scripts",
    "packs",
    "bin",
    "setup",
    ".claude-plugin",
    ".codex-plugin",
    ".devin-plugin",
    ".factory-plugin",
)
_VALIDATE_SENTINEL = Path("hooks") / "pre_tool_use.py"
_KEEP_VERSIONS = 2

_DEFAULT_CACHE_ROOTS = (
    Path.home() / ".codex" / "plugins" / "cache" / "unifable" / "unifable",
    Path.home() / ".claude" / "plugins" / "cache" / "unifable" / "unifable",
)

# Stable launchers. They resolve ~/.unifable/current at runtime and carry NO version or
# cache path, so they never go stale. UNIFABLE_HOME override keeps them testable.
_HOOK_BOOTSTRAP = """#!/usr/bin/env bash
# unifable hook bootstrap — managed by runtime_sync.py. Do not edit by hand.
set -uo pipefail
ROOT="${UNIFABLE_HOME:-$HOME/.unifable}/current"
script="${1:-}"
if [ -z "$script" ] || [ ! -f "$ROOT/hooks/$script" ]; then
  echo "{}"
  exit 0
fi
export PLUGIN_ROOT="$ROOT" CLAUDE_PLUGIN_ROOT="$ROOT" UNIFABLE_PLUGIN_ROOT="$ROOT"
case "$script" in
  *.sh) exec bash "$ROOT/hooks/$script" ;;
  *) exec python3 "$ROOT/hooks/$script" ;;
esac
"""

_CLI_BOOTSTRAP = """#!/usr/bin/env bash
# unifable CLI bootstrap — managed by runtime_sync.py. Do not edit by hand.
set -euo pipefail
ROOT="${UNIFABLE_HOME:-$HOME/.unifable}/current"
SPEC="$ROOT/scripts/gate/spec.py"
if [ ! -f "$SPEC" ]; then
  echo "unifable: runtime not found at $ROOT (run install/<host>.sh once to seed ~/.unifable)" >&2
  exit 1
fi
export PLUGIN_ROOT="$ROOT" CLAUDE_PLUGIN_ROOT="$ROOT" UNIFABLE_PLUGIN_ROOT="$ROOT"
exec python3 "$SPEC" "$@"
"""

_BOOTSTRAPS = {
    "unifable-hook": _HOOK_BOOTSTRAP,
    "unifable": _CLI_BOOTSTRAP,
    "unifable-spec": _CLI_BOOTSTRAP,
}


def _enabled() -> bool:
    val = os.environ.get("UNIFABLE_RUNTIME_SYNC", "").strip().lower()
    return val not in ("0", "false", "no", "off")


def unifable_home() -> Path:
    custom = os.environ.get("UNIFABLE_HOME", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".unifable"


def bindir() -> Path:
    custom = os.environ.get("UNIFABLE_BIN_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".local" / "bin"


def cache_roots() -> tuple[Path, ...]:
    raw = os.environ.get("UNIFABLE_CACHE_ROOTS", "").strip()
    if raw:
        return tuple(Path(p).expanduser() for p in raw.split(os.pathsep) if p)
    return _DEFAULT_CACHE_ROOTS


def _has_runtime(root: Path) -> bool:
    try:
        return (root / _VALIDATE_SENTINEL).is_file()
    except OSError:
        return False


def latest_cache_version(roots: tuple[Path, ...] | None = None):
    """Return (version_name, version_tuple, path) for the highest semver cache dir with a runtime."""
    best = None
    for parent in roots or cache_roots():
        try:
            if not parent.is_dir():
                continue
            entries = list(parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            ver = parse_version(entry.name)
            if ver is None or not _has_runtime(entry):
                continue
            if best is None or ver > best[1]:
                best = (entry.name, ver, entry.resolve())
    return best


def current_version(home: Path | None = None) -> str | None:
    """Name of the version ~/.unifable/current points at, or None if missing/invalid."""
    home = home or unifable_home()
    try:
        target = (home / "current").resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not _has_runtime(target):
        return None
    return target.name if parse_version(target.name) else None


def _py_compile_ok(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        proc = subprocess.run(
            [sys.executable or "python3", "-m", "py_compile", str(path)],
            capture_output=True,
            timeout=30,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _copy_version(src: Path, dest: Path) -> bool:
    """Copy the runtime subset of src into dest, validated. Returns True on success."""
    tmp = dest.parent / (dest.name + ".tmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        for name in _RUNTIME_TREE:
            s = src / name
            if not s.exists():
                continue
            d = tmp / name
            if s.is_dir():
                shutil.copytree(s, d, symlinks=False, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        if not _has_runtime(tmp) or not _py_compile_ok(tmp / _VALIDATE_SENTINEL):
            shutil.rmtree(tmp, ignore_errors=True)
            return False
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
        return True
    except (OSError, shutil.Error):
        shutil.rmtree(tmp, ignore_errors=True)
        return False


def _flip_current(home: Path, version_name: str) -> bool:
    """Atomically repoint ~/.unifable/current at versions/<version_name>."""
    target = home / "versions" / version_name
    if not _has_runtime(target):
        return False
    link = home / "current"
    tmp = home / ".current.tmp"
    try:
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(os.path.join("versions", version_name), tmp)  # relative -> relocatable
        os.replace(tmp, link)  # atomic symlink swap on POSIX
        return True
    except OSError:
        try:
            if tmp.is_symlink():
                tmp.unlink()
        except OSError:
            pass
        return False


def _write_bootstraps(home: Path, bdir: Path) -> None:
    """Write the stable ~/.unifable/bin launchers and link ~/.local/bin at them."""
    ubin = home / "bin"
    ubin.mkdir(parents=True, exist_ok=True)
    for name, content in _BOOTSTRAPS.items():
        path = ubin / name
        try:
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)
        except OSError:
            continue
    try:
        bdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for name in _BOOTSTRAPS:
        link = bdir / name
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            os.symlink(ubin / name, link)
        except OSError:
            continue


def _gc_versions(home: Path, keep: int = _KEEP_VERSIONS, protect: str | None = None) -> None:
    vdir = home / "versions"
    try:
        entries = [e for e in vdir.iterdir() if e.is_dir() and parse_version(e.name)]
    except OSError:
        return
    entries.sort(key=lambda e: parse_version(e.name), reverse=True)
    for index, entry in enumerate(entries):
        if index < keep or entry.name == protect:
            continue
        shutil.rmtree(entry, ignore_errors=True)


def sync_runtime(*, force: bool = False) -> bool:
    """Seed/refresh ~/.unifable from the latest cache version. Return True if `current` flipped."""
    if not _enabled():
        return False
    try:
        home = unifable_home()
        bdir = bindir()
        latest = latest_cache_version()
        cur = current_version(home)

        if latest is None:
            # No cache to sync from. Keep the existing runtime usable if we have one.
            if cur is not None:
                _write_bootstraps(home, bdir)
            return False

        latest_name, latest_tuple, latest_path = latest
        cur_tuple = parse_version(cur) if cur else None
        need = force or cur is None or (cur_tuple is not None and cur_tuple < latest_tuple)

        changed = False
        if need:
            dest = home / "versions" / latest_name
            if _has_runtime(dest) or _copy_version(latest_path, dest):
                changed = _flip_current(home, latest_name)

        _write_bootstraps(home, bdir)
        _gc_versions(home, protect=current_version(home))
        return changed
    except Exception:
        return False


def main() -> int:
    changed = sync_runtime(force="--force" in sys.argv[1:])
    print(json.dumps({"changed": changed, "current": current_version()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
