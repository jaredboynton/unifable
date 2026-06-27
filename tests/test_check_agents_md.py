#!/usr/bin/env python3
"""Tests for scripts/check_agents_md.py -- the AGENTS.md drift validator.

Each case builds a throwaway tree, points the module's ROOT/JUSTFILE at it, and
asserts the checker passes only when links and documented `just` recipes resolve.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import check_agents_md as cam  # noqa: E402


def _seed(tmp: Path, *, agents: str, justfile: str = "lint:\n    echo lint\n") -> None:
    (tmp / "AGENTS.md").write_text(agents)
    (tmp / "justfile").write_text(justfile)


def _point_at(monkeypatch: pytest.MonkeyPatch, tmp: Path) -> None:
    monkeypatch.setattr(cam, "ROOT", tmp)
    monkeypatch.setattr(cam, "JUSTFILE", tmp / "justfile")


def test_repo_passes_against_real_tree():
    assert cam.main() == 0


def test_passes_when_links_and_recipes_resolve(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# readme")
    _seed(tmp_path, agents="See [readme](README.md). Run `just lint`.\n")
    _point_at(monkeypatch, tmp_path)
    assert cam.main() == 0


def test_fails_on_broken_relative_link(tmp_path, monkeypatch):
    _seed(tmp_path, agents="See [gone](docs/missing.md).\n")
    _point_at(monkeypatch, tmp_path)
    assert cam.main() == 1


def test_fails_on_unknown_just_recipe_in_code_span(tmp_path, monkeypatch):
    _seed(tmp_path, agents="Run `just nonexistent` to ship.\n")
    _point_at(monkeypatch, tmp_path)
    assert cam.main() == 1


def test_ignores_just_in_prose(tmp_path, monkeypatch):
    _seed(tmp_path, agents="Adding a hook means more than just writing the file.\n")
    _point_at(monkeypatch, tmp_path)
    assert cam.main() == 0


def test_ignores_external_links_and_anchors(tmp_path, monkeypatch):
    _seed(
        tmp_path,
        agents="[site](https://example.com) [anchor](#section) [mail](mailto:a@b.c)\n",
    )
    _point_at(monkeypatch, tmp_path)
    assert cam.main() == 0


def test_just_keyword_args_are_not_recipes(tmp_path, monkeypatch):
    _seed(tmp_path, agents="Bump with `just patch` or `just minor`.\n")
    _point_at(monkeypatch, tmp_path)
    assert cam.main() == 0


def test_nested_agents_link_resolves_from_own_dir(tmp_path, monkeypatch):
    sub = tmp_path / "hooks"
    sub.mkdir()
    (sub / "wiring.md").write_text("# wiring")
    (sub / "AGENTS.md").write_text("See [wiring](wiring.md).\n")
    _seed(tmp_path, agents="root\n")
    _point_at(monkeypatch, tmp_path)
    assert cam.main() == 0


def _run_all() -> int:
    import tempfile

    class _MP:
        def __init__(self):
            self._saved = []

        def setattr(self, obj, name, value):
            self._saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in reversed(self._saved):
                setattr(obj, name, value)

    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for name, fn in tests:
        mp = _MP()
        try:
            with tempfile.TemporaryDirectory() as td:
                fn(Path(td), mp) if fn.__code__.co_argcount == 2 else fn()
            print(f"  OK  {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {name}: {exc}")
            failed += 1
        finally:
            mp.undo()
    print(f"\n{passed} passed, {failed} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
