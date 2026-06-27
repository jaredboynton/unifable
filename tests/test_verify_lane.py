#!/usr/bin/env python3
"""Tests for the breaker's async auto-grounding lane (scripts/gate/verify_lane.py).

Covers the safety boundary (sanction_command / sanction_tasks), the background
runner (run_verification_tasks), the sidecar read path, and dispatch (seed +
idempotency + detached spawn). Run: python3 -m pytest tests/test_verify_lane.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import verify_lane as vl  # noqa: E402


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "AGENTS.md").write_text(
        "Releases run `just test-all` before the version bump.\n"
        "Run pytest for focused checks. `make check` is also available.\n",
        encoding="utf-8",
    )
    return tmp_path


def test_sanction_accepts_policy_named_verification(tmp_path):
    repo = _repo(tmp_path)
    assert vl.sanction_command("just test-all", repo) is True
    assert vl.sanction_command("pytest", repo) is True
    assert vl.sanction_command("make check", repo) is True


def test_sanction_rejects_destructive_and_publishing(tmp_path):
    repo = _repo(tmp_path)
    assert vl.sanction_command("rm -rf build", repo) is False
    assert vl.sanction_command("git push origin main", repo) is False
    assert vl.sanction_command("just deploy-prod", repo) is False  # 'deploy' token
    assert vl.sanction_command("gh release create v1.0.0", repo) is False


def test_sanction_rejects_unnamed_command(tmp_path):
    repo = _repo(tmp_path)
    # verification-shaped but NOT named anywhere in repo policy -> dropped.
    assert vl.sanction_command("just frobnicate", repo) is False
    # bare python without a test runner module -> not verification-shaped.
    assert vl.sanction_command("python3 setup.py", repo) is False


def test_sanction_python_test_runner_requires_policy(tmp_path):
    repo = tmp_path
    (repo / "AGENTS.md").write_text("CI runs `python3 -m pytest` on every PR.\n", encoding="utf-8")
    assert vl.sanction_command("python3 -m pytest", repo) is True
    assert vl.sanction_command("python3 -m http.server", repo) is False


def test_sanction_tasks_filters_and_bounds(tmp_path):
    repo = _repo(tmp_path)
    raw = [
        {"subclaim": "tests pass", "command": "just test-all"},
        {"subclaim": "no docs", "command": "rm -rf docs"},  # dropped (destructive)
        {"subclaim": "missing cmd"},  # dropped (no command)
        {"subclaim": "dupe", "command": "just test-all"},  # dropped (duplicate)
    ]
    out = vl.sanction_tasks(raw, repo)
    assert out == [{"subclaim": "tests pass", "command": "just test-all"}]
    assert vl.sanction_tasks("not-a-list", repo) == []


def test_run_verification_tasks_records_exit_and_tail(tmp_path):
    out_path = tmp_path / "verify.json"
    vl.run_verification_tasks(["true", "false"], str(tmp_path), out_path)
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["status"] == "done"
    assert data["results"]["true"]["exit"] == 0
    assert data["results"]["false"]["exit"] == 1
    assert "finished_at" in data["results"]["true"]


def test_read_verification_results_failopen(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path / "data"))
    input_data = {"session_id": "S", "cwd": str(tmp_path)}
    # No sidecar yet -> empty, never raises.
    assert vl.read_verification_results(input_data, "deadbeef") == {}
    assert vl.read_verification_results(input_data, "") == {}


def test_dispatch_empty_tasks_returns_blank(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path / "data"))
    input_data = {"session_id": "S", "cwd": str(tmp_path)}
    assert vl.dispatch_verification(input_data, "claim", [], str(tmp_path)) == ""


def test_dispatch_seeds_sidecar_and_spawns(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path / "data"))
    input_data = {"session_id": "S", "cwd": str(tmp_path)}
    spawned = {}

    def _fake_spawn(out_path, cwd):
        spawned["out_path"] = str(out_path)
        spawned["cwd"] = str(cwd)

    monkeypatch.setattr(vl, "_spawn_runner", _fake_spawn)
    tasks = [{"subclaim": "tests pass", "command": "just test-all"}]
    key = vl.dispatch_verification(input_data, "release ok", tasks, str(tmp_path))
    assert key == vl.verify_key("release ok", str(tmp_path))
    seed = json.loads(vl._verify_path(input_data, key).read_text(encoding="utf-8"))
    assert seed["status"] == "running"
    assert seed["commands"] == ["just test-all"]
    assert spawned["out_path"] == str(vl._verify_path(input_data, key))


def test_dispatch_is_idempotent_for_same_repo_state(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path / "data"))
    input_data = {"session_id": "S", "cwd": str(tmp_path)}

    def _boom(*a, **k):
        raise AssertionError("must not spawn when sidecar already exists")

    key = vl.verify_key("claim", str(tmp_path))
    path = vl._verify_path(input_data, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "running", "commands": ["just test-all"], "results": {}}), encoding="utf-8")
    monkeypatch.setattr(vl, "_spawn_runner", _boom)
    tasks = [{"subclaim": "tests pass", "command": "just test-all"}]
    assert vl.dispatch_verification(input_data, "claim", tasks, str(tmp_path)) == key
