#!/usr/bin/env python3
"""Unit tests for scripts/gate/protected_paths.py.

protected_paths is extracted from hooks/pre_tool_use.py so the protected-root
guards can be tested directly, without spinning up the full PreToolUse hook. The
PROTECTED_PATHS integration coverage in tests/test_spec_gate.py stays as the
end-to-end proof; this file pins the unit contract.

Runs under pytest or standalone (python3 tests/test_protected_paths.py).
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import protected_paths as pp  # noqa: E402


def _with_data_root(dd: str):
    os.environ["UNIFABLE_DATA"] = dd


def test_is_protected_repo_local_unifable():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        target = str(Path(cwd) / ".unifable" / "findings.json")
        assert pp.is_protected(target, cwd) is True


def test_is_protected_global_spec_store():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        target = str(Path(dd) / "specs" / "abc" / "S1" / "spec.json")
        assert pp.is_protected(target, cwd) is True


def test_unprotected_ordinary_repo_file():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        target = str(Path(cwd) / "src" / "main.py")
        assert pp.is_protected(target, cwd) is False


def test_is_protected_write_for_edit_tool():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        protected = str(Path(cwd) / ".unifable" / "state" / "x.json")
        blocked, hit = pp.is_protected_write("Edit", {"file_path": protected}, cwd)
        assert blocked is True
        assert hit == protected

        ok = str(Path(cwd) / "README.md")
        blocked2, hit2 = pp.is_protected_write("Write", {"file_path": ok}, cwd)
        assert blocked2 is False
        assert hit2 is None


def test_is_protected_write_apply_patch_multi_file():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        spec = str(Path(dd) / "specs" / "abc" / "S1" / "spec.json")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: src/ok.py\n"
            "+print(1)\n"
            f"*** Update File: {spec}\n"
            "+{}\n"
            "*** End Patch\n"
        )
        blocked, hit = pp.is_protected_write("apply_patch", {"patch": patch}, cwd)
        assert blocked is True
        assert hit == spec


def test_bash_protected_write_redirect_and_sed():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        spec = str(Path(dd) / "specs" / "k" / "S" / "spec.json")
        assert pp.bash_protected_write(f"echo x > {spec}", cwd) is not None
        assert pp.bash_protected_write(f"sed -i 's/a/b/' {spec}", cwd) is not None
        assert pp.bash_protected_write(f"rm {spec}", cwd) is not None


def test_bash_protected_write_catches_redirect_variants():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        spec = str(Path(dd) / "specs" / "k" / "S" / "spec.json")
        quoted = f"'{spec}'"
        commands = [
            f"printf x >> {quoted}",
            f"cat <<'EOF' > {quoted}\nx\nEOF",
            f"python3 - <<'PY' > {quoted}\nprint('x')\nPY",
            f"cat >{quoted} <<'EOF'\nx\nEOF",
        ]
        for command in commands:
            assert pp.bash_protected_write(command, cwd) is not None, command


def test_bash_protected_write_catches_tee_sed_perl_variants():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        spec = str(Path(dd) / "specs" / "k" / "S" / "spec.json")
        commands = [
            f"tee {spec}",
            f"printf x | tee '{spec}'",
            f"printf x | tee -a '{spec}' >/dev/null",
            f"sed -i '' 's/a/b/' '{spec}'",
            f"sed -Ei 's/a/b/' '{spec}'",
            f"sed --in-place 's/a/b/' '{spec}'",
            f"perl -i -pe 's/a/b/' '{spec}'",
            f"perl --in-place -pe 's/a/b/' '{spec}'",
            f"perl -0777 -i -pe 's/a/b/s' '{spec}'",
        ]
        for command in commands:
            assert pp.bash_protected_write(command, cwd) is not None, command


def test_bash_protected_write_catches_dd_and_install_variants():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        spec = str(Path(dd) / "specs" / "k" / "S" / "spec.json")
        commands = [
            f"dd if=/dev/null of={spec}",
            f"install /dev/null {spec}",
            f"install -D /dev/null '{spec}'",
            f"install /dev/null --target-directory={Path(spec).parent}",
        ]
        for command in commands:
            assert pp.bash_protected_write(command, cwd) is not None, command


def test_bash_protected_write_catches_repo_local_relative_unifable():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        assert pp.bash_protected_write("echo x > .unifable/state.json", cwd) is not None
        assert pp.bash_protected_write("printf x | tee .unifable/findings.json", cwd) is not None


def test_bash_protected_write_catches_allowed_command_redirects():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        state = str(Path(cwd) / ".unifable" / "state.json")
        spec = str(Path(dd) / "specs" / "k" / "S" / "spec.json")
        commands = [
            f"find . -maxdepth 1 -type f > {state}",
            f"pytest -q > {spec}",
            f"python -m pytest -q >> {state}",
            f"rg --files | sed -n '1,10p' > {spec}",
        ]
        for command in commands:
            assert pp.bash_protected_write(command, cwd) is not None, command


def test_bash_protected_write_ignores_nonmutating_and_unprotected():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as dd:
        _with_data_root(dd)
        spec = str(Path(dd) / "specs" / "k" / "S" / "spec.json")
        # reading a protected file is fine (non-mutating command)
        assert pp.bash_protected_write(f"cat {spec}", cwd) is None
        # mutating an ordinary file is fine
        ok = str(Path(cwd) / "out.txt")
        assert pp.bash_protected_write(f"echo x > {ok}", cwd) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: protected_paths unit contract")
