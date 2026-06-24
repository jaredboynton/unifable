#!/usr/bin/env python3
"""Auto-heal the unifable CLI symlink on UserPromptSubmit.

Probes the installed `unifable` on PATH and re-runs setup/install-bin.sh when the
symlink is missing, broken, non-executable, points at a stale plugin root, or
targets an older plugin version than the loaded plugin.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_MANIFESTS = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".devin-plugin/plugin.json",
    ".factory-plugin/plugin.json",
)
_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")
_CACHE_VERSION_RE = re.compile(r"/unifable/unifable/(\d+\.\d+\.\d+)/")


def _auto_heal_enabled() -> bool:
    val = os.environ.get("UNIFABLE_CLI_AUTO_HEAL", "").strip().lower()
    return val not in ("0", "false", "no", "off")


def bindir() -> Path:
    custom = os.environ.get("UNIFABLE_BIN_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".local" / "bin"


def parse_version(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    match = _VERSION_RE.search(str(text))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def read_plugin_version(root: Path) -> str | None:
    for rel in _MANIFESTS:
        manifest = root / rel
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()
    return None


def resolve_plugin_root(explicit: Path | None = None) -> Path | None:
    try:
        from plugin_root import resolve_plugin_root as resolve_effective_root
    except ImportError:  # pragma: no cover
        from scripts.gate.plugin_root import resolve_plugin_root as resolve_effective_root
    return resolve_effective_root(explicit)


def _resolve_symlink_target(path: Path) -> Path | None:
    try:
        if not path.is_symlink() and not path.exists():
            return None
        return path.resolve(strict=False)
    except OSError:
        return None


def _plugin_root_from_cli_target(target: Path | None) -> Path | None:
    if target is None:
        return None
    parent = target.parent
    if parent.name != "bin":
        return None
    return parent.parent.resolve()


def _version_from_cache_path(path: Path) -> str | None:
    match = _CACHE_VERSION_RE.search(str(path))
    if match:
        return match.group(1)
    return None


@dataclass(frozen=True)
class CurrentCliContext:
    plugin_root: Path
    version: str | None
    version_tuple: tuple[int, ...] | None


@dataclass(frozen=True)
class InstalledCliState:
    bindir: Path
    command_path: Path | None
    symlink_path: Path | None
    target_path: Path | None
    plugin_root: Path | None
    version: str | None
    version_tuple: tuple[int, ...] | None
    executable: bool
    broken: bool


def current_cli_context(plugin_root: Path | None = None) -> CurrentCliContext | None:
    root = resolve_plugin_root(plugin_root)
    if root is None or not root.is_dir():
        return None
    version = read_plugin_version(root)
    return CurrentCliContext(
        plugin_root=root,
        version=version,
        version_tuple=parse_version(version),
    )


def _probe_command(bdir: Path, name: str) -> tuple[Path | None, Path | None, Path | None, bool, bool]:
    """Return (command_path, symlink_path, target_path, executable, broken)."""
    symlink_path = bdir / name
    command_path: Path | None = None
    if symlink_path.is_symlink() or symlink_path.exists():
        command_path = symlink_path
    if command_path is None:
        return None, None, None, False, True
    target_path = _resolve_symlink_target(command_path)
    broken = target_path is None or not target_path.exists()
    executable = bool(
        target_path is not None
        and target_path.is_file()
        and os.access(target_path, os.X_OK)
    )
    return command_path, symlink_path if symlink_path.is_symlink() else None, target_path, executable, broken


def probe_installed_cli(*, bindir_override: Path | None = None) -> InstalledCliState:
    bdir = (bindir_override or bindir()).expanduser()
    command_path, symlink_path, target_path, executable, broken = _probe_command(bdir, "unifable")
    hook_path, hook_symlink, hook_target, hook_exec, hook_broken = _probe_command(bdir, "unifable-hook")

    if command_path is None:
        unifable_link = bdir / "unifable"
        return InstalledCliState(
            bindir=bdir,
            command_path=None,
            symlink_path=unifable_link if unifable_link.exists() or unifable_link.is_symlink() else None,
            target_path=None,
            plugin_root=None,
            version=None,
            version_tuple=None,
            executable=False,
            broken=True,
        )

    if hook_path is None or hook_broken or not hook_exec:
        broken = True
    elif hook_target is not None and target_path is not None:
        hook_root = _plugin_root_from_cli_target(hook_target)
        cli_root = _plugin_root_from_cli_target(target_path)
        if hook_root and cli_root and hook_root.resolve() != cli_root.resolve():
            broken = True

    plugin_root = _plugin_root_from_cli_target(target_path)
    version = read_plugin_version(plugin_root) if plugin_root else None
    if version is None and target_path is not None:
        version = _version_from_cache_path(target_path)

    return InstalledCliState(
        bindir=bdir,
        command_path=command_path,
        symlink_path=symlink_path if symlink_path.is_symlink() else None,
        target_path=target_path,
        plugin_root=plugin_root,
        version=version,
        version_tuple=parse_version(version),
        executable=executable,
        broken=broken,
    )


def needs_heal(current: CurrentCliContext, installed: InstalledCliState) -> bool:
    if installed.command_path is None:
        return True
    if installed.broken:
        return True
    if not installed.executable:
        return True
    if installed.plugin_root is None:
        return True
    if installed.plugin_root.resolve() != current.plugin_root.resolve():
        return True
    if (
        current.version_tuple is not None
        and installed.version_tuple is not None
        and installed.version_tuple < current.version_tuple
    ):
        return True
    if current.version_tuple is not None and installed.version_tuple is None:
        return True
    return False


def cli_install_state(plugin_root: Path | None = None) -> dict:
    current = current_cli_context(plugin_root)
    installed = probe_installed_cli()
    if current is None:
        return {
            "current": None,
            "installed": installed.__dict__,
            "needs_heal": False,
        }
    return {
        "current": {
            "plugin_root": str(current.plugin_root),
            "version": current.version,
            "version_tuple": current.version_tuple,
        },
        "installed": {
            "bindir": str(installed.bindir),
            "command_path": str(installed.command_path) if installed.command_path else None,
            "symlink_path": str(installed.symlink_path) if installed.symlink_path else None,
            "target_path": str(installed.target_path) if installed.target_path else None,
            "plugin_root": str(installed.plugin_root) if installed.plugin_root else None,
            "version": installed.version,
            "version_tuple": installed.version_tuple,
            "executable": installed.executable,
            "broken": installed.broken,
        },
        "needs_heal": needs_heal(current, installed),
    }


def _run_install_bin(plugin_root: Path) -> bool:
    script = plugin_root / "setup" / "install-bin.sh"
    if not script.is_file():
        return False
    env = os.environ.copy()
    env.setdefault("UNIFABLE_BIN_DIR", str(bindir()))
    try:
        proc = subprocess.run(
            ["bash", str(script), str(plugin_root)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def ensure_cli(*, plugin_root: Path | None = None) -> bool:
    """Ensure PATH has a current, executable unifable CLI. Fail open; return True if healed."""
    if not _auto_heal_enabled():
        return False
    try:
        current = current_cli_context(plugin_root)
        if current is None:
            return False
        installed = probe_installed_cli()
        if not needs_heal(current, installed):
            return False
        return _run_install_bin(current.plugin_root)
    except Exception:
        return False
