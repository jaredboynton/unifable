#!/usr/bin/env python3
"""Tests for scripts/gate/db.py, the consolidated SQLite gate store.

Covers schema bootstrap, the session+activity split, breaker events, spec doc
roundtrips, atomic per-project finding id minting, and the sacred fail-open
contract (a corrupt or unwritable DB must degrade to empty/None, never raise).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import db  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    return tmp_path


def test_schema_bootstrap_sets_user_version(tmp_path):
    with db.connect() as conn:
        assert conn is not None
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert version == db.SCHEMA_VERSION
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sessions", "activity", "breaker", "breaker_events", "specs", "projects", "findings"} <= tables
    assert (tmp_path / "unifable.db").exists()


def test_session_roundtrip_splits_activity_and_dedups():
    led = {
        "grade": "STANDARD",
        "read_paths": ["/a/b.py", "/a/b.py", "/a/c.py"],  # dup collapses
        "fetched_urls": ["https://x/y"],
        "ran_commands": ["pytest -q"],
        "tool_evidence": ["mcp: foo"],
        "pretool_block_counts": {"bash": 2},
    }
    db.session_save("S1", led, session_id="sid", project_root="/repo")
    out = db.session_load("S1")
    assert out is not None
    assert out["grade"] == "STANDARD"
    assert out["pretool_block_counts"] == {"bash": 2}
    assert sorted(out["read_paths"]) == ["/a/b.py", "/a/c.py"]
    assert out["fetched_urls"] == ["https://x/y"]
    assert out["tool_evidence"] == ["mcp: foo"]


def test_session_load_absent_is_none():
    assert db.session_load("does-not-exist") is None


def test_activity_add_is_idempotent():
    db.activity_add("S2", "read_paths", ["/p/q.py", "/p/q.py"])
    db.activity_add("S2", "read_paths", ["/p/q.py"])
    out = db.session_load("S2")
    assert out is not None
    assert out["read_paths"] == ["/p/q.py"]


def test_breaker_events_roundtrip():
    state = {
        "breaker_armed": True,
        "breaker_claim": "the claim",
        "events": [
            {"kind": "ARM", "ts": "t1", "claim": "c"},
            {"kind": "DISARM", "ts": "t2", "reason": "grounded"},
        ],
    }
    db.breaker_save("B1", state)
    out = db.breaker_load("B1")
    assert out is not None
    assert out["breaker_armed"] is True
    assert out["breaker_claim"] == "the claim"
    assert [e["kind"] for e in out["events"]] == ["ARM", "DISARM"]
    assert out["events"][1]["reason"] == "grounded"


def test_breaker_absent_is_none():
    assert db.breaker_load("nope") is None


def test_spec_roundtrip_and_keys_and_delete():
    doc = {"restated_goal": "g", "tasks": [{"id": "T1", "status": "validated"}]}
    db.spec_save("h1/sess", doc)
    assert db.spec_load("h1/sess") == doc
    assert "h1/sess" in db.spec_keys()
    db.spec_delete("h1/sess")
    assert db.spec_load("h1/sess") is None


def test_finding_add_mints_per_project_sequence():
    fid1 = db.finding_add("rhA", "/repoA", "sql-injection", "SQLi", "high")
    fid2 = db.finding_add("rhA", "/repoA", "memory-leak", "Leak", "medium")
    # A different project restarts the counter at 1.
    fid_b = db.finding_add("rhB", "/repoB", "race", "Race", "critical")
    assert fid1 == "sql-injection-1"
    assert fid2 == "memory-leak-2"
    assert fid_b == "race-1"
    loaded = db.findings_load("rhA")
    assert loaded["counter"] == 2
    assert set(loaded["findings"]) == {"sql-injection-1", "memory-leak-2"}


def test_finding_set_status_updates_and_raises_on_missing():
    fid = db.finding_add("rhC", "/repoC", "bug", "Bug", "high")
    updated = db.finding_set_status("rhC", fid, "resolved", resolution="patched")
    assert updated["status"] == "resolved"
    assert updated["resolution"] == "patched"
    with pytest.raises(KeyError):
        db.finding_set_status("rhC", "no-such-9", "resolved", resolution="x")


def test_findings_replace_rewrites_set():
    db.finding_add("rhD", "/repoD", "first", "First", "low")
    data = {
        "counter": 5,
        "findings": {
            "manual-5": {
                "title": "Manual",
                "severity": "critical",
                "status": "open",
                "source": "",
                "location": "",
                "evidence": "",
                "resolution": "",
                "verify_cmd": "",
                "verify_evidence": "",
                "created": "t",
            }
        },
    }
    db.findings_replace("rhD", "/repoD", data)
    out = db.findings_load("rhD")
    assert out["counter"] == 5
    assert set(out["findings"]) == {"manual-5"}


def test_fail_open_on_corrupt_db(tmp_path):
    # A garbage file at the DB path must not raise: connect() moves it aside and
    # recreates, so accessors degrade to empty/None rather than wedging a session.
    (tmp_path / "unifable.db").write_bytes(b"this is not a sqlite database at all")
    assert db.session_load("anything") is None  # must not raise
    # After recreation a normal write/read works.
    db.session_save("R1", {"grade": "X"})
    assert db.session_load("R1")["grade"] == "X"
    assert (tmp_path / "unifable.db.corrupt").exists()


def test_accessors_fail_open_when_data_root_unwritable(monkeypatch):
    # Point the DB at an unwritable path: every accessor must return its safe
    # default instead of raising into a hook.
    monkeypatch.setattr(db, "db_path", lambda: Path("/proc/nonexistent/cannot/unifable.db"))
    assert db.session_load("x") is None
    assert db.breaker_load("x") is None
    assert db.spec_load("x") is None
    assert db.spec_keys() == []
    assert db.findings_load("x") == {"findings": {}, "counter": 0}
    assert db.finding_add("x", "/r", "s", "t", "high") is None
    # Writes simply no-op (no exception).
    db.session_save("x", {"grade": "Y"})
    db.activity_add("x", "read_paths", ["/a"])


def test_immediate_write_rolls_back_on_error():
    # A raising op inside a write transaction must not leave partial state, and
    # the wrapper returns the default rather than propagating.
    def boom(conn: sqlite3.Connection):
        conn.execute("INSERT INTO sessions(skey, data, updated_at) VALUES('T','{}','now')")
        raise RuntimeError("boom")

    result = db._write(boom, "DEFAULT")
    assert result == "DEFAULT"
    with db.connect() as conn:
        row = conn.execute("SELECT 1 FROM sessions WHERE skey='T'").fetchone()
    assert row is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
