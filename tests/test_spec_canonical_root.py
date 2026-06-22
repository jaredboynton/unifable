#!/usr/bin/env python3
"""Canonical project root spec keying: subdirs share one spec; fragmented specs relocate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

from spec import (  # noqa: E402
    canonical_project_root,
    dir_hash,
    load_spec,
    save_spec,
    spec_path,
    spec_template,
    _cmd_where,
    _apply_cli_context,
    _safe_session,
)


def _with_data(tmp: str):
    os.environ["UNIFABLE_DATA"] = tmp


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)


def _spec_with_task() -> dict:
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "Ship the feature"
    spec["goal_seeded"] = False
    spec["tasks"] = [{"id": "T1", "title": "works", "check": "true", "status": "pending"}]
    return spec


def test_subdir_shares_dirhash_with_repo_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    sub = repo / "skills" / "foo"
    sub.mkdir(parents=True)
    assert dir_hash(repo) == dir_hash(sub)
    assert canonical_project_root(sub) == canonical_project_root(repo).resolve()


def test_save_in_subdir_visible_from_root(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _with_data(str(data))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    sub = repo / "pkg"
    sub.mkdir()
    spec = _spec_with_task()
    save_spec(sub, "sess-a", spec)
    loaded = load_spec(repo, "sess-a")
    assert loaded is not None
    assert loaded["tasks"][0]["id"] == "T1"
    assert spec_path(repo, "sess-a") == spec_path(sub, "sess-a")


def test_relocate_fragmented_spec(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _with_data(str(data))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    sub = repo / "nested"
    sub.mkdir()

    canonical = dir_hash(repo)
    wrong_hash = "deadbeefcafebabe"
    assert wrong_hash != canonical

    fragmented = data / "specs" / wrong_hash / _safe_session("sess-r") / "spec.json"
    fragmented.parent.mkdir(parents=True)
    spec = _spec_with_task()
    fragmented.write_text(json.dumps(spec), encoding="utf-8")

    loaded = load_spec(repo, "sess-r")
    assert loaded is not None
    assert loaded["tasks"][0]["title"] == "works"
    assert spec_path(repo, "sess-r").exists()
    assert not fragmented.exists()


def test_apply_cli_context_resolves_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-session-1")
    args = type("Args", (), {"cmd": "add-task"})()
    assert _apply_cli_context(args) is None
    assert args.task_id == "env-session-1"
    assert args.root == str(canonical_project_root(repo))


def test_where_shows_location(tmp_path, capsys, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _with_data(str(data))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-w")
    monkeypatch.setenv("UNIFABLE_DEV", "1")
    save_spec(repo, "sess-w", _spec_with_task())
    rc = _cmd_where(type("Args", (), {"root": str(repo), "task_id": "sess-w"})())
    captured = capsys.readouterr()
    out = captured.out
    err = captured.err
    assert rc == 0
    assert "session-id: sess-w" in out
    assert "dirhash:" in out
    assert "not your session-id" in out
    assert "[--] T1" in out
    # diagnostic for empirical env validation is emitted on stderr
    assert "UNIFABLE_SESSION_RESOLVED=" in err
    assert "SOURCE=" in err
