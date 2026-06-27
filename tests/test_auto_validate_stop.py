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
import spec_judge  # noqa: E402
import spec_stop_validate as ssv  # noqa: E402
from spec import all_tasks_validated, auto_validate_spec, load_spec, save_spec, spec_template  # noqa: E402
from spec_cli import _cmd_dispute
from spec_stop_validate import _apply_check_result  # noqa: E402


def _task(tid, status, **extra):
    t = {"id": tid, "title": tid, "check": "true", "status": status}
    t.update(extra)
    return t


def _seed_repo_cite_file(tmp_path: Path) -> None:
    """repo_context cites must resolve to an existing path after spec hygiene."""
    (tmp_path / "a.py").write_text("# fixture\n", encoding="utf-8")


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

    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".": (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks", fake_judge_tasks)
    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert calls["n"] == 1


def test_auto_validate_runs_checks_in_parallel(tmp_path, monkeypatch):
    import threading
    import time

    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [
        _task("T1", "pending", check="true"),
        _task("T2", "pending", check="true"),
        _task("T3", "pending", check="true"),
    ]
    save_spec(str(tmp_path), "K", s)
    lock = threading.Lock()
    active = {"n": 0, "max": 0}

    def slow_run_check(check, cwd=".", timeout=None):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        deadline = time.monotonic() + 0.12
        while time.monotonic() < deadline:
            pass
        with lock:
            active["n"] -= 1
        return 0, "ok"

    monkeypatch.setattr(ssv, "run_check", slow_run_check)
    monkeypatch.setattr(ssv, "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items],
    )
    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert active["max"] >= 2


def test_failed_always_reruns_check(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "failed", exit=1, output="prior failure")]
    save_spec(str(tmp_path), "K", s)
    seen = {"check": False}

    def fake_run_check(check, cwd=".", timeout=None):
        seen["check"] = True
        return 0, "fresh ok"

    monkeypatch.setattr(ssv, "run_check", fake_run_check)
    monkeypatch.setattr(ssv, "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items],
    )
    spec, msgs = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert seen["check"] is True
    assert spec["tasks"][0]["status"] == "validated"
    assert any("check re-run: exit 0 (was 1)" in m for m in msgs)


def test_failed_replay_failed_flag_skips_rerun(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "failed", exit=1, output="prior failure", replay_failed=True)]
    save_spec(str(tmp_path), "K", s)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("run_check must not run when replay_failed is set")

    monkeypatch.setattr(ssv, "run_check", fail_if_called)
    monkeypatch.setattr(ssv, "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items],
    )
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert spec["tasks"][0]["status"] == "validated"


def test_failed_empty_output_reruns_and_validates(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "failed", exit=1, output="")]
    save_spec(str(tmp_path), "K", s)
    seen = {"check": False}

    def fake_run_check(check, cwd=".", timeout=None):
        seen["check"] = True
        return 0, ""

    monkeypatch.setattr(ssv, "run_check", fake_run_check)
    monkeypatch.setattr(ssv, "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items],
    )
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert seen["check"] is True
    assert spec["tasks"][0]["status"] == "validated"


def test_revise_applies_verdict_same_stop(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "failed", exit=1, output="bad", check="false")]
    save_spec(str(tmp_path), "K", s)

    def fake_judge_tasks(sp, items, *, transcript="", **kw):
        spec_judge._apply_adjustments(
            sp,
            {
                "adjust_requirements": [
                    {
                        "id": "T1",
                        "action": "revise",
                        "check": "true",
                        "reason": "check was wrong",
                    }
                ],
            },
        )
        return [(1, "ok", [], "") for _ in items]

    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks", fake_judge_tasks)
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    task = spec["tasks"][0]
    assert task["check"] == "true"
    assert task["status"] == "validated"
    assert task["exit"] == 0
    assert task["output"] == "ok"


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

    monkeypatch.setattr(ssv, "run_check", fake_run_check)
    monkeypatch.setattr(ssv, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items])
    auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert seen["check"] is True


def test_auto_validate_passes_pending_task(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items])
    spec, msgs = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    assert spec["tasks"][0]["status"] == "validated"
    assert all_tasks_validated(spec)[0] is True
    assert msgs


def test_validated_runnable_check_records_evidence(tmp_path, monkeypatch):
    """A task validated off a runnable check records HOW (validated_by = cmd + exit)."""
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending", check="rg -q foo bar.py")]
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items])
    spec, msgs = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    t = spec["tasks"][0]
    assert t["status"] == "validated"
    assert t.get("validated_by") == "`rg -q foo bar.py` (exit 0)"
    assert any("validated by `rg -q foo bar.py` (exit 0)" in m for m in msgs)


def test_validated_prose_check_has_no_command_evidence(tmp_path, monkeypatch):
    """A prose (non-runnable) acceptance criterion validates without a validated_by command."""
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task("T1", "pending", check="Slack search returned a relevant direct message")]
    save_spec(str(tmp_path), "K", s)
    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items])
    spec, _ = auto_validate_spec(load_spec(str(tmp_path), "K"), str(tmp_path))
    t = spec["tasks"][0]
    assert t["status"] == "validated"
    assert not t.get("validated_by")


def test_front_failures_do_not_starve_back_tasks(tmp_path, monkeypatch):
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = [_task(f"T{i}", "pending") for i in range(1, 8)]
    save_spec(str(tmp_path), "K", s)

    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))

    def fake_judge_tasks(sp, items, *, transcript="", **kw):
        out = []
        for it in items:
            tid = it["task"]["id"]
            if tid in {"T1", "T2", "T3"}:
                out.append((0, f"{tid} still failing", [], ""))
            else:
                out.append((1, f"{tid} ok", [], ""))
        return out

    monkeypatch.setattr(ssv, "judge_tasks", fake_judge_tasks)
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
    monkeypatch.setattr(ssv, "judge_dispute", lambda sp, t, e: (1, "accepted"))
    monkeypatch.setattr(ssv, "judge_tasks",
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
    _seed_repo_cite_file(tmp_path)
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(1, "ok", [], "") for _ in items])

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") != "block"
    assert out == {}
    assert load_spec(str(tmp_path), "sess")["tasks"][0]["status"] == "validated"
    digest = tmp_path / "specs"
    matches = list(digest.rglob("last_stop_validation.txt")) if digest.is_dir() else []
    assert matches, "expected persisted stop digest on passthrough"
    assert "Spec complete: all tasks validated." in matches[0].read_text(encoding="utf-8")


def test_stop_passthrough_empty_when_breaker_open(tmp_path, monkeypatch):
    """Clean allow-stop emits {} — no additionalContext that re-engages the session."""
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    _seed_repo_cite_file(tmp_path)
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
    _seed_repo_cite_file(tmp_path)
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
    monkeypatch.setattr(ssv, "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [(0, reason, [], "") for _ in items],
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    block_reason = out.get("reason") or ""
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    # reason carries the alarm plus Action lines; full digest rides additionalContext.
    assert "Completion gate blocked" in block_reason
    assert reason in ctx
    assert "Action required:" in ctx
    assert "T1 [XX]" in ctx or "T1:" in ctx


def test_stop_board_not_duplicated_into_reason(tmp_path, monkeypatch):
    """The spec board rides additionalContext only; reason keeps just the alarm."""
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    _seed_repo_cite_file(tmp_path)
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(
        ssv, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(0, "T1 needs more proof", [], "") for _ in items]
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    block_reason = out.get("reason") or ""
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "Completion gate blocked" in block_reason
    assert "T1 needs more proof" in ctx


def test_stop_forwards_three_task_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    _seed_repo_cite_file(tmp_path)
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

    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(ssv, "judge_tasks", fake_judge_tasks)

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    for tid in ("T1", "T2", "T3"):
        assert f"{tid} lacks evidence" in ctx


def test_stop_persists_digest_and_reason_hints(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    _seed_repo_cite_file(tmp_path)
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [
        {
            "id": "T1",
            "title": "proof",
            "check": "true",
            "status": "pending",
        }
    ]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (0, "ok"))
    monkeypatch.setattr(ssv, "judge_tasks",
        lambda sp, items, *, transcript="", **kw: [
            (
                0,
                "non-probative; run the behavioral test",
                [],
                "",
            )
            for _ in items
        ],
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    block_reason = out.get("reason") or ""
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "run the behavioral test" in ctx
    assert "Action required:" in ctx
    assert "run the behavioral test" in ctx
    digest = tmp_path / "specs"
    matches = list(digest.rglob("last_stop_validation.txt")) if digest.is_dir() else []
    assert matches, "expected persisted stop digest"
    assert "Action required:" in matches[0].read_text(encoding="utf-8")


def test_stop_validate_context_builder_failopen_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    _seed_repo_cite_file(tmp_path)
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "pending")]
    save_spec(str(tmp_path), "sess", s)
    monkeypatch.setattr(ssv, "run_check", lambda check, cwd=".", timeout=None: (1, "fail"))
    monkeypatch.setattr(ssv, "judge_tasks", lambda sp, items, *, transcript="", **kw: [(0, "no", [], "") for _ in items])
    monkeypatch.setattr(
        gate_stop,
        "_build_stop_validate_context",
        lambda spec, val_msgs, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    out = _run_stop(gate_stop, {"session_id": "sess", "cwd": str(tmp_path)})
    assert out.get("decision") == "block"
    assert "Completion gate blocked" in (out.get("reason") or "")


def test_stop_resolves_transcript_without_payload_path(tmp_path, monkeypatch):
    monkeypatch.setenv("UNIFABLE_VERIFY_CITATIONS", "0")
    import gate_stop
    from transcript_locate import _encode_cwd

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    _seed_repo_cite_file(tmp_path)
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    cwd = str(tmp_path)
    session = "sess-transcript"
    fake_home = tmp_path / "home"
    proj = fake_home / ".claude" / "projects" / _encode_cwd(cwd)
    proj.mkdir(parents=True)
    transcript_file = proj / f"{session}.jsonl"
    transcript_file.write_text(
        '{"type":"user","message":{"role":"user","content":"hello"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(fake_home))
    captured: dict[str, str | None] = {}

    def fake_auto_validate(spec, cwd_arg, **kw):
        captured["transcript_path"] = kw.get("transcript_path")
        return spec, []

    s = spec_template()
    s["requires_tasks"] = True
    s["restated_goal"] = "ship"
    s["repo_context"] = [{"cite": "a.py:1", "why": "read this session"}]
    s["prior_art"] = [{"cite": "https://example.com", "why": "fetched this session"}]
    s["tasks"] = [_task("T1", "validated")]
    save_spec(str(tmp_path), session, s)
    monkeypatch.setattr(ssv, "auto_validate_spec", fake_auto_validate)

    _run_stop(gate_stop, {"session_id": session, "cwd": cwd})
    assert captured.get("transcript_path") == str(transcript_file)


def test_apply_check_result_supersedes_primary_when_adopted(tmp_path):
    from spec import append_frontier_task, set_primary_task

    s = spec_template()
    s["heavy_workflow"] = True
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = []
    append_frontier_task(s, "Frontier A", "true")
    append_frontier_task(s, "Frontier B", "true")
    set_primary_task(s, "Primary", "true")
    f1, _ = [t for t in s["tasks"] if t.get("approach_kind") == "frontier"]
    f1["status"] = "accepted_approach"
    f1["comparison_winner"] = True
    primary = next(t for t in s["tasks"] if t.get("approach_kind") == "primary")
    primary["status"] = "pending"
    _apply_check_result(s, primary, 0, "ok", 1, "passed", [])
    assert primary["status"] == "superseded"
    assert "adopted frontier" in primary["judge_reason"]


def test_loop_release_returns_recomputed_incomplete(tmp_path, monkeypatch):
    """_handle_completion_loop_release must not return a stale incomplete list."""
    import gate_stop
    from spec import append_frontier_task, set_primary_task

    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    s = spec_template()
    s["heavy_workflow"] = True
    s["requires_tasks"] = True
    s["restated_goal"] = "g"
    s["tasks"] = []
    append_frontier_task(s, "Frontier A", "true")
    append_frontier_task(s, "Frontier B", "true")
    set_primary_task(s, "Primary", "true")
    f1, f2 = [t for t in s["tasks"] if t.get("approach_kind") == "frontier"]
    f1["status"] = "accepted_approach"
    f1["comparison_winner"] = True
    f2["status"] = "rejected_approach"
    primary = next(t for t in s["tasks"] if t.get("approach_kind") == "primary")
    primary["status"] = "validated"
    save_spec(str(tmp_path), "heal", s)
    from ledger import load_ledger

    led = load_ledger({"session_id": "heal", "cwd": str(tmp_path)})
    stale_incomplete = ["T4"]
    spec, ok_tasks, incomplete, _, _, early = gate_stop._handle_completion_loop_release(
        {"session_id": "heal", "cwd": str(tmp_path)},
        str(tmp_path),
        "heal",
        load_spec(str(tmp_path), "heal"),
        led,
        stale_incomplete,
        [],
        "",
    )
    assert early is None
    assert ok_tasks is True
    assert incomplete == []
    healed_primary = next(t for t in spec["tasks"] if t.get("approach_kind") == "primary")
    assert healed_primary["status"] == "superseded"
