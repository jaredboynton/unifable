#!/usr/bin/env python3
"""Tests for the runtime allowlist (docs/benchmarks/python-consolidation-runtime-allowlist.json).

The allowlist is the per-path override file the inventory classifier consults
for files no rule covers. These tests pin its shape: every entry names a real
file, a valid class, and a non-empty owner+reason; every override actually wins
over the rule defaults; and no entry is stale (points at a missing file).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import audit_runtime_inventory as inv  # noqa: E402


def _entries() -> list[dict]:
    data = json.loads(inv.ALLOWLIST.read_text(encoding="utf-8"))
    return data["entries"]


def test_allowlist_file_is_valid_json_with_entries():
    entries = _entries()
    assert entries, "expected a non-empty allowlist"


def test_every_entry_has_required_fields_and_valid_class():
    for e in _entries():
        assert e.get("path", "").strip(), f"entry missing path: {e}"
        assert e.get("classification") in inv.CLASSES, f"{e['path']}: bad class {e.get('classification')}"
        assert e.get("owner", "").strip(), f"{e['path']}: empty owner"
        assert e.get("reason", "").strip(), f"{e['path']}: empty reason"


def test_no_stale_entries_point_at_missing_files():
    for e in _entries():
        assert (REPO / e["path"]).is_file(), f"allowlist entry points at missing file: {e['path']}"


def test_overrides_win_over_rule_defaults():
    rows, problems = inv.build_inventory(REPO)
    assert problems == []
    by_path = {r.path: r for r in rows}
    for e in _entries():
        row = by_path.get(e["path"])
        assert row is not None, f"allowlisted path absent from inventory: {e['path']}"
        assert row.classification == e["classification"], f"{e['path']}: override not applied"
        assert row.owner == e["owner"]
        assert row.reason == e["reason"]


def test_missing_allowlisted_file_is_reported(tmp_path, monkeypatch):
    bogus = tmp_path / "allow.json"
    bogus.write_text(
        json.dumps(
            {"entries": [{"path": "skills/nope/ghost.sh", "classification": "fixture", "owner": "x", "reason": "y"}]}
        )
    )
    monkeypatch.setattr(inv, "ALLOWLIST", bogus)
    _, problems = inv.build_inventory(REPO)
    assert any("missing file" in p and "skills/nope/ghost.sh" in p for p in problems)
