#!/usr/bin/env python3
"""Tests for scripts/bump_version.py -- in particular that `just version` keeps
the concrete `just version X.Y.Z` example in AGENTS.md in sync, without touching
the `just version <X.Y.Z>` angle-bracket form or the patch|minor|major keywords.

Pattern-level checks plus an end-to-end run on a throwaway tree (REPO is
monkeypatched, so no real manifests are touched).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import bump_version as bv  # noqa: E402

# ---------------------------------------------------------------------------
# Managed-set wiring
# ---------------------------------------------------------------------------


def test_agents_md_is_managed():
    assert "AGENTS.md" in bv.MANAGED
    entry = [t for t in bv.TARGETS if t[0] == "AGENTS.md"][0]
    # AGENTS.md is bumped via the just-version pattern, not the JSON field one.
    assert entry[1] is bv.JUST_VERSION


def test_all_manifests_still_managed():
    for rel in (
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        ".devin-plugin/plugin.json",
        ".factory-plugin/plugin.json",
        ".factory-plugin/marketplace.json",
    ):
        assert rel in bv.MANAGED, rel


# ---------------------------------------------------------------------------
# Pattern discrimination
# ---------------------------------------------------------------------------


def test_just_version_matches_concrete_example():
    m = bv.JUST_VERSION.search("just version 1.2.3")
    assert m and m.group(2) == "1.2.3"


def test_just_version_ignores_angle_form_and_keywords():
    assert bv.JUST_VERSION.search("just version <X.Y.Z>") is None
    assert bv.JUST_VERSION.search("just version patch") is None
    assert bv.JUST_VERSION.search("just version minor") is None
    assert bv.JUST_VERSION.search("just version major") is None


def test_version_field_captures_semver():
    m = bv.VERSION_FIELD.search('  "version": "1.9.4",')
    assert m and m.group(2) == "1.9.4"


# ---------------------------------------------------------------------------
# End-to-end on a throwaway tree
# ---------------------------------------------------------------------------


def test_rejects_explicit_downgrade():
    old_repo = bv.REPO
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".claude-plugin").mkdir()
        (tmp / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "1.9.30"}))
        bv.REPO = tmp
        try:
            with pytest.raises(SystemExit, match="downgrades are not allowed"):
                bv.main(["1.9.29"])
        finally:
            bv.REPO = old_repo


def test_rejects_same_explicit_version():
    old_repo = bv.REPO
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".claude-plugin").mkdir()
        (tmp / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "1.9.30"}))
        bv.REPO = tmp
        try:
            with pytest.raises(SystemExit, match="already the current version"):
                bv.main(["1.9.30"])
        finally:
            bv.REPO = old_repo


def test_end_to_end_syncs_agents_md_and_manifest():
    old_repo = bv.REPO
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".claude-plugin").mkdir()
        (tmp / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "1.9.4"}))
        agents = tmp / "AGENTS.md"
        agents.write_text(
            "just version 1.9.4          # or: just version patch|minor|major\n"
            "run `just version <X.Y.Z>` (or `just version patch|minor|major`)\n"
        )
        bv.REPO = tmp
        try:
            rc = bv.main(["1.9.5"])
            # Read inside the with-block, before the temp tree is removed.
            out = agents.read_text()
            manifest = json.loads((tmp / ".claude-plugin" / "plugin.json").read_text())
        finally:
            bv.REPO = old_repo

    assert rc == 0
    assert "just version 1.9.5" in out  # concrete example bumped
    assert "just version <X.Y.Z>" in out  # angle-bracket form untouched
    assert "just version patch|minor|major" in out  # keyword form untouched
    assert "1.9.4" not in out  # no stale concrete version left
    assert manifest["version"] == "1.9.5"


# ---------------------------------------------------------------------------
# Runner (standalone, mirrors the other test files)
# ---------------------------------------------------------------------------


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  OK  {fn.__name__}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {fn.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
