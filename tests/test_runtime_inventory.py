#!/usr/bin/env python3
"""Tests for scripts/audit_runtime_inventory.py -- the canonical runtime classifier.

The classifier is the single source of truth for which shipped files are active
runtime paths versus shims/legacy/archived/fixtures. These tests pin its contract:
every non-Python runtime file gets exactly one valid class with a non-empty
owner+reason, the real tree classifies cleanly, and the --fail-on-active gate
trips on a forbidden token sitting on an active row.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import audit_runtime_inventory as inv  # noqa: E402


def test_real_tree_classifies_cleanly():
    rows, problems = inv.build_inventory(REPO)
    assert problems == [], f"unexpected classification problems: {problems}"
    assert rows, "expected a non-empty inventory"


def test_every_nonpy_row_has_one_valid_class_and_owner_reason():
    rows, _ = inv.build_inventory(REPO)
    for r in rows:
        if not r.is_nonpy:
            continue
        assert r.classification in inv.CLASSES, f"{r.path}: bad class {r.classification}"
        assert r.owner.strip(), f"{r.path}: empty owner"
        assert r.reason.strip(), f"{r.path}: empty reason"


def test_known_paths_classify_as_expected():
    rows, _ = inv.build_inventory(REPO)
    by_path = {r.path: r for r in rows}
    # Active unitrace implementation.
    assert by_path["skills/unitrace/scripts/enhance-prompt.mjs"].classification == "active"
    # Archived variant.
    assert by_path["skills/unitrace/scripts/archive/trace-gemini.sh"].classification == "archived"
    # Stable launcher shim.
    assert by_path["bin/unifable-hook"].classification == "compat-shim"
    # Allowlisted router shim.
    assert by_path["hooks/router.sh"].classification == "compat-shim"


def test_fail_on_active_trips_while_mjs_present(capsys):
    # The pre-migration tree has active .mjs, so the gate must fail.
    rc = inv.main(["--fail-on-active", ".mjs"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "active row carries forbidden" in err


def test_plain_audit_passes():
    assert inv.main([]) == 0


def test_unclassified_file_is_a_problem(tmp_path, monkeypatch):
    # Point the audit at a throwaway tree with an un-ruled, un-allowlisted file.
    (tmp_path / "skills" / "other" / "scripts").mkdir(parents=True)
    stray = tmp_path / "skills" / "other" / "scripts" / "thing.mjs"
    stray.write_text("console.log(1)\n")
    monkeypatch.setattr(inv, "ALLOWLIST", tmp_path / "nope.json")
    rows, problems = inv.build_inventory(tmp_path)
    assert any("unclassified" in p for p in problems)


def test_write_artifact_roundtrips(tmp_path):
    out = tmp_path / "inv.json"
    rc = inv.main(["--write-artifact", str(out)])
    assert rc == 0
    import json

    data = json.loads(out.read_text())
    assert set(data) >= {"trees", "counts", "rows", "problems"}
    assert data["counts"]
