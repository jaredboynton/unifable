#!/usr/bin/env python3
"""Harness citation sanitization and gate-defect filtering."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "gate"
sys.path.insert(0, str(GATE))

from citations import (  # noqa: E402
    HARNESS_READ_WHY,
    empty_activity,
    filter_gate_defect_citation_reasons,
    sanitize_harness_citations,
    scan_transcript,
    sync_citations_from_activity,
    verify_citations,
)
from spec import spec_template  # noqa: E402


def test_sanitize_removes_harness_auto_sync_for_missing_file(tmp_path):
    spec = spec_template()
    spec["repo_context"] = [
        {"cite": ".claude-plugin/hooks.json:1", "why": HARNESS_READ_WHY},
        {"cite": "hooks/hooks.json:1", "why": "binding for plugin wiring"},
    ]
    removed = sanitize_harness_citations(spec, str(tmp_path))
    assert ".claude-plugin/hooks.json:1" in removed
    cites = [item["cite"] for item in spec["repo_context"]]
    assert ".claude-plugin/hooks.json:1" not in cites
    assert "hooks/hooks.json:1" in cites


def test_sync_skips_nonexistent_paths(tmp_path):
    spec = spec_template()
    activity = empty_activity()
    activity["read_paths"] = [str(tmp_path / "missing.py")]
    assert sync_citations_from_activity(spec, activity, str(tmp_path)) is False
    assert spec.get("repo_context") == []


def test_filter_gate_defect_drops_phantom_verify_reason(tmp_path):
    spec = spec_template()
    spec["repo_context"] = [
        {"cite": "nope/missing.py:1", "why": HARNESS_READ_WHY},
    ]
    reasons = verify_citations(spec, empty_activity(), str(tmp_path), require_commands=False)
    assert reasons
    filtered = filter_gate_defect_citation_reasons(spec, reasons, str(tmp_path))
    assert filtered == []


def test_scan_transcript_skips_failed_read(tmp_path):
    transcript = tmp_path / "session.jsonl"
    entry = {
        "cwd": str(tmp_path),
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": str(tmp_path / "ghost.py")},
                },
                {
                    "type": "tool_result",
                    "content": "File does not exist",
                },
            ]
        },
    }
    transcript.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    act = scan_transcript(str(transcript))
    assert act["read_paths"] == []


def _run_pre_tool(payload, data_dir):
    env = os.environ.copy()
    env["UNIFABLE_DATA"] = data_dir
    env["UNIFABLE_GRADE"] = "STANDARD"
    proc = subprocess.run(
        [sys.executable, str(REPO / "hooks" / "pre_tool_use.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=env,
        cwd=str(REPO),
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_pretool_sanitizes_phantom_and_allows_edit(tmp_path):
    from spec import save_spec, spec_template

    real = tmp_path / "src.py"
    real.write_text("x = 1\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    sess = "sess"

    spec = spec_template()
    spec["restated_goal"] = "Fix the thing properly with evidence"
    spec["goal_seeded"] = False
    spec["repo_context"] = [
        {"cite": "phantom/missing.py:1", "why": HARNESS_READ_WHY},
        {"cite": f"{real.name}:1", "why": HARNESS_READ_WHY},
    ]
    spec["prior_art"] = [{"cite": "https://example.com/x", "why": "fetched this session"}]
    spec["acceptance_criteria"] = [{"check": "true", "evidence": "ok"}]

    old_data = os.environ.get("UNIFABLE_DATA")
    os.environ["UNIFABLE_DATA"] = str(data)
    try:
        save_spec(tmp_path, sess, spec)
    finally:
        if old_data is None:
            os.environ.pop("UNIFABLE_DATA", None)
        else:
            os.environ["UNIFABLE_DATA"] = old_data

    ledger_dir = data / "ledgers"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    from ledger import ledger_key

    payload_base = {"session_id": sess, "cwd": str(tmp_path)}
    ledger = {
        "read_paths": [str(real.resolve())],
        "fetched_urls": ["https://example.com/x"],
    }
    (ledger_dir / f"{ledger_key(payload_base)}.json").write_text(
        json.dumps(ledger),
        encoding="utf-8",
    )

    payload = {
        **payload_base,
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "out.py")},
    }
    rc, out, err = _run_pre_tool(payload, str(data))
    assert rc == 0, err
    out_obj = json.loads(out or "{}")
    from spec import load_spec

    os.environ["UNIFABLE_DATA"] = str(data)
    updated = load_spec(tmp_path, sess)
    assert updated is not None
    cites = [c["cite"] for c in updated.get("repo_context", [])]
    assert "phantom/missing.py:1" not in cites
    assert f"{real.name}:1" in cites
    assert load_spec(tmp_path, sess) is not None
