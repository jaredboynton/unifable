#!/usr/bin/env python3
"""auto_validate_spec: harness runs checks+judge on stop (no validate-task CLI)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import spec as spec_mod  # noqa: E402
from spec import (  # noqa: E402
    all_tasks_validated,
    auto_validate_spec,
    load_spec,
    save_spec,
    spec_template,
    _cmd_dispute,
)


def _task(tid, status, **extra):
    t = {"id": tid, "title": tid, "check": "true", "status": status}
    t.update(extra)
    return t


def test_auto_validate_one_judge_call_for_all_tasks(tmp_path, monkeypatch):
    """Every open task (validate + dispute) goes through a single judge_tasks call."""
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [
        _task("T1", "pending"),
        _task("T2", "failed", exit=1, output="prior"),
        _task("T3", "disputed", dispute_evidence="blocked upstream"),
    ]
    save_spec(str(tmp_path), "K", s)
    calls = {"n": 0}

    def fake_judge_tasks(sp, items, *, transcript="", **kw):
        calls["n"] += 1
        assert len(items) == 3
        kinds = {it.get("kind") for it in items}
        assert kinds == {"validate", "dispute"}
        return [(1, "ok", [], "") for _ in items]

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_tasks", fake_judge_tasks)
    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert calls["n"] == 1


def test_failed_rejudged_without_check_rerun(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "failed", exit=1, output="prior failure")]
    save_spec(str(tmp_path), "K", s)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("run_check must not run for failed tasks with stored output")

    monkeypatch.setattr(spec_mod, "run_check", fail_if_called)
    monkeypatch.setattr(
        spec_mod, "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items],
    )
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert spec["tasks"][0]["status"] == "validated"


def test_pending_still_runs_check(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "K", s)
    seen = {"check": False}

    def fake_run_check(check, cwd=".", timeout=None):
        seen["check"] = True
        return 0, "ok"

    monkeypatch.setattr(spec_mod, "run_check", fake_run_check)
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items])
    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert seen["check"] is True


def test_auto_validate_passes_pending_task(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items])
    spec, msgs = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert spec["tasks"][0]["status"] == "validated"
    assert all_tasks_validated(spec)[0] is True
    assert msgs


def test_front_failures_do_not_starve_back_tasks(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task(f"T{i}", "pending") for i in range(1, 8)]
    save_spec(str(tmp_path), "K", s)

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))

    def fake_judge_tasks(sp, items, *, transcript="", **kw):
        out = []
        for it in items:
            tid = it["task"]["id"]
            if tid in {"T1", "T2", "T3"}:
                out.append((0, f"{tid} still failing", [], ""))
            else:
                out.append((1, f"{tid} ok", [], ""))
        return out

    monkeypatch.setattr(spec_mod, "judge_tasks", fake_judge_tasks)
    spec = load_spec(str(tmp_path), "K")
    spec, _ = auto_validate_spec(spec, str(tmp_path))

    by_id = {t["id"]: t for t in spec["tasks"]}
    assert [by_id[f"T{i}"]["status"] for i in range(4, 8)] == ["validated"] * 4
    assert all(by_id[f"T{i}"]["attempts"] >= 1 for i in range(4, 8))
    assert all(by_id[f"T{i}"]["status"] == "failed" for i in range(1, 4))


def test_auto_validate_adjudicates_dispute(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["tasks"] = [_task("T1", "failed")]
    save_spec(str(tmp_path), "K", s)
    args = SimpleNamespace(root=str(tmp_path), task_id="K", task="T1", evidence="impossible")
    _cmd_dispute(args)
    monkeypatch.setattr(spec_mod, "judge_dispute", lambda sp, t, e: (1, "accepted"))
    monkeypatch.setattr(
        spec_mod, "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(1, "accepted", [], "") for _ in items],
    )
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert spec["tasks"][0]["status"] == "retracted"


def _run_stop(gate_stop, payload):
    captured = {"out": {}}
    gate_stop.read_stdin_json = lambda: payload
    gate_stop.emit_json = lambda d: captured.__setitem__("out", d)
    gate_stop.main()
    return captured["out"]


def test_stop_runs_auto_validate_before_breaker_check(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items])

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") != "block"
    assert out == {}
    assert load_spec(str(tmp_path), "sess")["tasks"][0]["status"] == "validated"
    digest = tmp_path / "specs"
    matches = list(digest.rglob("last_stop_validation.txt")) if digest.is_dir() else []
    assert matches, "expected persisted stop digest on passthrough"
    assert "breaker: OPEN" in matches[0].read_text(encoding="utf-8")


def test_stop_passthrough_empty_when_breaker_open(tmp_path, monkeypatch):
    """Clean allow-stop emits {} — no additionalContext that re-engages the session."""
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "validated"), _task("T2", "validated")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(
        spec_mod,
        "auto_validate_spec",
        lambda spec, cwd, **kw: (spec, ["T2 validated"]),
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out == {}
    assert "hookSpecificOutput" not in out
    assert "decision" not in out


def test_stop_forwards_dispute_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "failed")]
    save_spec(str(tmp_path), "sess", s)
    args = SimpleNamespace(root=str(tmp_path), task_id="sess", task="T1", evidence="not possible")
    _cmd_dispute(args)
    reason = "Rejected. The evidence does not prove impossibility."
    monkeypatch.setattr(
        spec_mod, "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(0, reason, [], "") for _ in items],
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    block_reason = out.get("reason") or ""
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    # reason carries the alarm plus Action lines; full digest rides additionalContext.
    assert "breaker CLOSED" in block_reason
    assert reason in ctx
    assert "Action:" in block_reason
    assert "T1:" in block_reason
    assert "unifable spec update" not in block_reason
    assert "T1 [XX] T1" in ctx


def test_stop_board_not_duplicated_into_reason(tmp_path, monkeypatch):
    """The spec board rides additionalContext only; reason keeps just the alarm."""
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(
        spec_mod, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(0, "T1 needs more proof", [], "") for _ in items]
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    block_reason = out.get("reason") or ""
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "breaker CLOSED" in block_reason
    assert "T1 needs more proof" in ctx


def test_stop_forwards_three_task_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending"), _task("T2", "pending"), _task("T3", "pending")]
    save_spec(str(tmp_path), "sess", s)

    def fake_judge_tasks(sp, items, *, transcript="", **kw):
        out = []
        for it in items:
            tid = it["task"]["id"]
            out.append((0, f"{tid} lacks evidence", [], ""))
        return out

    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(spec_mod, "judge_tasks", fake_judge_tasks)

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    for tid in ("T1", "T2", "T3"):
        assert f"{tid} lacks evidence" in ctx


def test_stop_persists_digest_and_reason_hints(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [{
        "id": "T1",
        "title": "proof",
        "check": "true",
        "status": "pending",
    }]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(
        spec_mod,
        "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(
            0,
            "non-probative; run the behavioral test",
            [],
            "",
        ) for _ in items],
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    block_reason = out.get("reason") or ""
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "run the behavioral test" in ctx
    assert "Action:" in block_reason
    assert "run the behavioral test" in block_reason
    assert "Action required:" in ctx
    digest = tmp_path / "specs"
    matches = list(digest.rglob("last_stop_validation.txt")) if digest.is_dir() else []
    assert matches, "expected persisted stop digest"
    assert "Action required:" in matches[0].read_text(encoding="utf-8")


def test_stop_validate_context_builder_failopen_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(spec_mod, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(spec_mod, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(0, "no", [], "") for _ in items])
    monkeypatch.setattr(
        gate_stop,
        "_build_stop_validate_context",
        lambda spec, val_msgs, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    assert "breaker CLOSED" in (out.get("reason") or "")
