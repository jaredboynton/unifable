#!/usr/bin/env python3
"""Citation auto-sync from hook activity (gap 1).

Covers the two properties the HEAVY frontier checks select on:
- ledger_hook: reads/fetches recorded in ledger activity sync into the spec's
  repo_context / prior_art, and added_sink names exactly what was appended (this
  is what drives the PostToolUse "synced N cite(s)" headline).
- idempotent: replaying the same activity appends nothing and reports no change,
  so a wide read does not re-announce or duplicate cites.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

from citations import empty_activity, sync_citations_from_activity  # noqa: E402
from spec import repo_context_of, spec_template  # noqa: E402


def _activity(reads=(), fetches=()):
    act = empty_activity()
    act["read_paths"] = list(reads)
    act["fetched_urls"] = list(fetches)
    return act


def test_ledger_hook_driven_sync_appends_and_names_cites(tmp_path):
    f = tmp_path / "src" / "mod.py"
    f.parent.mkdir(parents=True)
    f.write_text("# x\n")
    spec = spec_template()
    activity = _activity(reads=[str(f.resolve())], fetches=["https://docs.example.com/g"])
    sink: dict[str, list[str]] = {}

    assert sync_citations_from_activity(spec, activity, str(tmp_path), added_sink=sink) is True
    repo_cites = [item["cite"] for item in repo_context_of(spec)]
    prior_cites = [item.get("cite") for item in spec.get("prior_art", [])]
    assert any("mod.py" in c for c in repo_cites)
    assert "https://docs.example.com/g" in prior_cites
    # The sink names exactly what this call appended (drives the gap-1 headline).
    assert any("mod.py" in c for c in sink.get("repo_context", []))
    assert "https://docs.example.com/g" in sink.get("prior_art", [])


def test_idempotent_replay_appends_nothing(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("# x\n")
    spec = spec_template()
    activity = _activity(reads=[str(f.resolve())], fetches=["https://x.io/p"])

    assert sync_citations_from_activity(spec, activity, str(tmp_path)) is True
    before_repo = len(spec.get("repo_context", []))
    before_prior = len(spec.get("prior_art", []))

    # Replaying identical activity is a no-op: no change, empty sink, no duplicates.
    sink: dict[str, list[str]] = {}
    assert sync_citations_from_activity(spec, activity, str(tmp_path), added_sink=sink) is False
    assert sink == {}
    assert len(spec.get("repo_context", [])) == before_repo
    assert len(spec.get("prior_art", [])) == before_prior


def test_repl_activity_idempotent_replay_appends_nothing(tmp_path):
    f = tmp_path / "src" / "mod.py"
    f.parent.mkdir(parents=True)
    f.write_text("# x\n")
    spec = spec_template()
    activity = _activity(reads=[str(f.resolve())])

    assert sync_citations_from_activity(spec, activity, str(tmp_path)) is True
    before = len(spec.get("repo_context", []))

    sink: dict[str, list[str]] = {}
    assert sync_citations_from_activity(spec, activity, str(tmp_path), added_sink=sink) is False
    assert sink == {}
    assert len(spec.get("repo_context", [])) == before
