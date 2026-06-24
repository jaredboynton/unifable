#!/usr/bin/env python3
"""Resolve the unifable plugin root, including stale-cache fallback.

Codex and Claude inject PLUGIN_ROOT / CLAUDE_PLUGIN_ROOT pointing at a versioned
cache directory. After a plugin upgrade the old directory may be removed while a
live session still references it. Callers that must survive upgrades scan for the
highest semver install under known cache roots when the env path is missing hooks.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from cli_install import parse_version
except ImportError:  # pragma: no cover
    from scripts.gate.cli_install import parse_version

_HOOK_SENTINEL = Path("hooks") / "pre_tool_use.py"
_CACHE_ROOTS = (
    Path.home() / ".codex" / "plugins" / "cache" / "unifable" / "unifable",
    Path.home() / ".claude" / "plugins" / "cache" / "unifable" / "unifable",
)
_ENV_VARS = ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "UNIFABLE_PLUGIN_ROOT")


def _root_has_hooks(root: Path) -> bool:
    try:
        return (root / _HOOK_SENTINEL).is_file()
    except OSError:
        return False


def _latest_cache_root() -> Path | None:
    best: Path | None = None
    best_ver: tuple[int, ...] | None = None
    for cache_parent in _CACHE_ROOTS:
        if not cache_parent.is_dir():
            continue
        try:
            entries = list(cache_parent.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            ver = parse_version(entry.name)
            if ver is None or not _root_has_hooks(entry):
                continue
            if best_ver is None or ver > best_ver:
                best_ver = ver
                best = entry.resolve()
    return best


def resolve_plugin_root(explicit: Path | None = None) -> Path | None:
    """Return plugin root with hooks, preferring env then latest cache semver."""
    if explicit is not None:
        root = explicit.expanduser().resolve()
        return root if _root_has_hooks(root) else None

    for var in _ENV_VARS:
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        root = Path(raw).expanduser().resolve()
        if _root_has_hooks(root):
            return root

    return _latest_cache_root()
