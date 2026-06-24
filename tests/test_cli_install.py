#!/usr/bin/env python3
"""Tests for scripts/gate/cli_install.py — CLI auto-heal on UserPromptSubmit."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

from cli_install import (  # noqa: E402
    CurrentCliContext,
    InstalledCliState,
    ensure_cli,
    needs_heal,
    parse_version,
    probe_installed_cli,
)


def _write_manifest(root: Path, version: str) -> None:
    manifest_dir = root / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "plugin.json").write_text(
        json.dumps({"name": "unifable", "version": version}),
        encoding="utf-8",
    )


def _write_cli_tree(root: Path, *, version: str, executable: bool = True) -> None:
    _write_manifest(root, version)
    scripts = root / "scripts" / "gate"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "spec.py").write_text("# stub\n", encoding="utf-8")
    hooks = root / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre_tool_use.py").write_text("# stub\n", encoding="utf-8")
    setup = root / "setup"
    setup.mkdir(parents=True, exist_ok=True)
    install_src = REPO / "setup" / "install-bin.sh"
    (setup / "install-bin.sh").write_text(
        install_src.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("unifable", "unifable-spec", "unifable-hook"):
        target = bin_dir / name
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        mode = stat.S_IRWXU if executable else stat.S_IRUSR | stat.S_IWUSR
        target.chmod(mode)


class TestParseVersion(unittest.TestCase):
    def test_parse_semver(self) -> None:
        self.assertEqual(parse_version("1.9.27"), (1, 9, 27))
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertIsNone(parse_version(""))

    def test_compare_order(self) -> None:
        self.assertLess(parse_version("1.9.25"), parse_version("1.9.27"))


class TestNeedsHeal(unittest.TestCase):
    def _current(self, root: Path, version: str) -> CurrentCliContext:
        return CurrentCliContext(
            plugin_root=root,
            version=version,
            version_tuple=parse_version(version),
        )

    def test_missing_command(self) -> None:
        current = self._current(Path("/tmp/current"), "1.9.27")
        installed = InstalledCliState(
            bindir=Path("/tmp/bin"),
            command_path=None,
            symlink_path=None,
            target_path=None,
            plugin_root=None,
            version=None,
            version_tuple=None,
            executable=False,
            broken=True,
        )
        self.assertTrue(needs_heal(current, installed))

    def test_broken_symlink(self) -> None:
        current = self._current(Path("/tmp/current"), "1.9.27")
        installed = InstalledCliState(
            bindir=Path("/tmp/bin"),
            command_path=Path("/tmp/bin/unifable"),
            symlink_path=Path("/tmp/bin/unifable"),
            target_path=None,
            plugin_root=None,
            version=None,
            version_tuple=None,
            executable=False,
            broken=True,
        )
        self.assertTrue(needs_heal(current, installed))

    def test_non_executable(self) -> None:
        root = Path("/tmp/current")
        target = root / "bin" / "unifable"
        current = self._current(root, "1.9.27")
        installed = InstalledCliState(
            bindir=Path("/tmp/bin"),
            command_path=Path("/tmp/bin/unifable"),
            symlink_path=Path("/tmp/bin/unifable"),
            target_path=target,
            plugin_root=root,
            version="1.9.27",
            version_tuple=(1, 9, 27),
            executable=False,
            broken=False,
        )
        self.assertTrue(needs_heal(current, installed))

    def test_stale_version(self) -> None:
        current_root = Path("/tmp/current")
        installed_root = Path("/tmp/old")
        current = self._current(current_root, "1.9.27")
        installed = InstalledCliState(
            bindir=Path("/tmp/bin"),
            command_path=Path("/tmp/bin/unifable"),
            symlink_path=Path("/tmp/bin/unifable"),
            target_path=installed_root / "bin" / "unifable",
            plugin_root=installed_root,
            version="1.9.25",
            version_tuple=(1, 9, 25),
            executable=True,
            broken=False,
        )
        self.assertTrue(needs_heal(current, installed))

    def test_current_and_healthy(self) -> None:
        root = Path("/tmp/current")
        current = self._current(root, "1.9.27")
        installed = InstalledCliState(
            bindir=Path("/tmp/bin"),
            command_path=Path("/tmp/bin/unifable"),
            symlink_path=Path("/tmp/bin/unifable"),
            target_path=root / "bin" / "unifable",
            plugin_root=root,
            version="1.9.27",
            version_tuple=(1, 9, 27),
            executable=True,
            broken=False,
        )
        self.assertFalse(needs_heal(current, installed))


class TestProbeAndEnsureCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(os.environ["TEST_TMPDIR"]) if "TEST_TMPDIR" in os.environ else None

    def _make_dirs(self) -> tuple[Path, Path, Path]:
        import tempfile

        base = Path(tempfile.mkdtemp(prefix="unifable-cli-heal-"))
        bindir = base / "bindir"
        current = base / "current"
        old = base / "old"
        bindir.mkdir()
        current.mkdir()
        old.mkdir()
        return bindir, current, old

    def test_probe_missing(self) -> None:
        bindir, current, _old = self._make_dirs()
        _write_cli_tree(current, version="1.9.27")
        state = probe_installed_cli(bindir_override=bindir)
        self.assertIsNone(state.command_path)
        self.assertTrue(state.broken)

    def test_probe_stale_symlink(self) -> None:
        bindir, current, old = self._make_dirs()
        _write_cli_tree(current, version="1.9.27")
        _write_cli_tree(old, version="1.9.25")
        link = bindir / "unifable"
        link.symlink_to(old / "bin" / "unifable")
        state = probe_installed_cli(bindir_override=bindir)
        ctx = CurrentCliContext(
            plugin_root=current,
            version="1.9.27",
            version_tuple=(1, 9, 27),
        )
        self.assertTrue(needs_heal(ctx, state))
        self.assertEqual(state.version, "1.9.25")

    def test_probe_current_executable(self) -> None:
        bindir, current, _old = self._make_dirs()
        _write_cli_tree(current, version="1.9.27", executable=True)
        (bindir / "unifable").symlink_to(current / "bin" / "unifable")
        (bindir / "unifable-hook").symlink_to(current / "bin" / "unifable-hook")
        state = probe_installed_cli(bindir_override=bindir)
        ctx = CurrentCliContext(
            plugin_root=current,
            version="1.9.27",
            version_tuple=(1, 9, 27),
        )
        self.assertFalse(needs_heal(ctx, state))

    def test_probe_non_executable(self) -> None:
        bindir, current, _old = self._make_dirs()
        _write_cli_tree(current, version="1.9.27", executable=False)
        link = bindir / "unifable"
        link.symlink_to(current / "bin" / "unifable")
        state = probe_installed_cli(bindir_override=bindir)
        ctx = CurrentCliContext(
            plugin_root=current,
            version="1.9.27",
            version_tuple=(1, 9, 27),
        )
        self.assertTrue(needs_heal(ctx, state))

    def test_probe_broken_symlink(self) -> None:
        bindir, _current, _old = self._make_dirs()
        link = bindir / "unifable"
        link.symlink_to(bindir / "missing-target")
        state = probe_installed_cli(bindir_override=bindir)
        self.assertTrue(state.broken)

    def test_ensure_cli_heals_into_bindir(self) -> None:
        bindir, current, old = self._make_dirs()
        _write_cli_tree(current, version="1.9.27")
        _write_cli_tree(old, version="1.9.25")
        (bindir / "unifable").symlink_to(old / "bin" / "unifable")

        with mock.patch.dict(
            os.environ,
            {"UNIFABLE_BIN_DIR": str(bindir)},
            clear=False,
        ):
            for key in ("CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT", "UNIFABLE_PLUGIN_ROOT"):
                os.environ.pop(key, None)
            healed = ensure_cli(plugin_root=current)

        self.assertTrue(healed)
        target = (bindir / "unifable").resolve()
        self.assertEqual(target, (current / "bin" / "unifable").resolve())
        self.assertTrue(os.access(target, os.X_OK))
        hook_target = (bindir / "unifable-hook").resolve()
        self.assertEqual(hook_target, (current / "bin" / "unifable-hook").resolve())
        self.assertTrue(os.access(hook_target, os.X_OK))

    def test_ensure_cli_respects_opt_out(self) -> None:
        bindir, current, _old = self._make_dirs()
        _write_cli_tree(current, version="1.9.27")
        env = os.environ.copy()
        env["UNIFABLE_BIN_DIR"] = str(bindir)
        env["UNIFABLE_CLI_AUTO_HEAL"] = "0"

        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os; "
                    f"os.environ['UNIFABLE_BIN_DIR']={str(bindir)!r}; "
                    "os.environ['UNIFABLE_CLI_AUTO_HEAL']='0'; "
                    "from cli_install import ensure_cli; "
                    f"raise SystemExit(0 if ensure_cli(plugin_root={str(current)!r}) is False else 1)"
                ),
            ],
            cwd=str(REPO / "scripts" / "gate"),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((bindir / "unifable").exists())


if __name__ == "__main__":
    unittest.main()
