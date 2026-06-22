#!/usr/bin/env python3
"""sync_citations_from_activity: hook-driven evidence from ledger activity."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

from citations import empty_activity, sync_citations_from_activity  # noqa: E402
from spec import repo_context_of, spec_template  # noqa: E402


def test_sync_appends_repo_context_from_reads(tmp_path):
    cwd = str(tmp_path)
    f = tmp_path / "src" / "mod.py"
    f.parent.mkdir(parents=True)
    f.write_text("# x\n")
    abs_path = str(f.resolve())
    spec = spec_template()
    activity = empty_activity()
    activity["read_paths"] = [abs_path]
    assert sync_citations_from_activity(spec, activity, cwd) is True
    cites = [item["cite"] for item in repo_context_of(spec)]
    assert any("mod.py" in c for c in cites)
    assert spec["repo_context"][0]["why"] == "read this session"
    # idempotent
    assert sync_citations_from_activity(spec, activity, cwd) is False


def test_sync_appends_prior_art_from_fetches(tmp_path):
    cwd = str(tmp_path)
    spec = spec_template()
    url = "https://docs.example.com/guide"
    activity = empty_activity()
    activity["fetched_urls"] = [url]
    assert sync_citations_from_activity(spec, activity, cwd) is True
    assert spec["prior_art"][0]["cite"] == url
    assert spec["prior_art"][0]["why"] == "fetched this session"
