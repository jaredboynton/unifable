#!/usr/bin/env python3
"""PostToolUse judge fan-out: concurrency, budget, coalesce, and delta-merge.

Covers the parallelization pieces independent of a live judge:
  - run_judges_parallel runs jobs concurrently, abandons a job past the budget
    (fail-open), and drops a raising job.
  - run_posttool_judges packs the four named results into a PosttoolResult.
  - claim_spec_judging coalesces sibling spec-judging within the window.
  - posttool_background.run_reconcile_job merges reconcile + frontier deltas onto one
    base spec under update_spec's lock (reconcile first, frontier ids minted after ->
    no collision) and enqueues the resulting context for the next PreToolUse.
  - update_spec is a locked read-modify-write that returns None when no spec exists.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import db  # noqa: E402
from posttool_judges import (  # noqa: E402
    claim_spec_judging,
    run_judges_parallel,
    run_posttool_judges,
)
from spec import load_spec, save_spec, spec_template  # noqa: E402
from spec_io import update_spec  # noqa: E402


def _write_transcript(path: Path, *user_lines: str) -> None:
    """Minimal JSONL transcript with human user turns (one line per turn)."""
    import json

    records = []
    for text in user_lines:
        records.append(json.dumps({"message": {"role": "user", "content": text}}))
    path.write_text("\n".join(records) + "\n", encoding="utf-8")


def _payload_with_transcript(cwd: str, session_id: str, transcript: Path) -> dict:
    return {"session_id": session_id, "cwd": cwd, "transcript_path": str(transcript)}


# --- run_judges_parallel ----------------------------------------------------


def test_run_judges_parallel_collects_completed():
    out = run_judges_parallel({"a": lambda: 1, "b": lambda: "two"}, budget=2.0)
    assert out == {"a": 1, "b": "two"}


def test_run_judges_parallel_runs_concurrently():
    # A 2-party barrier completes only if both jobs run at the same time; under
    # serial execution the first job blocks on the barrier until it breaks. The
    # threading.Barrier makes the concurrency claim deterministic with no pacing delay.
    barrier = threading.Barrier(2, timeout=2.0)

    def job():
        barrier.wait()
        return "ok"

    out = run_judges_parallel({"a": job, "b": job}, budget=2.0)
    assert out == {"a": "ok", "b": "ok"}


def test_run_judges_parallel_abandons_slow_job():
    # A job blocked on an Event the test never sets must be abandoned at the budget,
    # not awaited. threading.Event.wait (bounded) stands in for a hung judge.
    stuck = threading.Event()

    def slow():
        stuck.wait(timeout=5.0)
        return "late"

    start = time.monotonic()
    out = run_judges_parallel({"slow": slow, "fast": lambda: "quick"}, budget=0.3)
    elapsed = time.monotonic() - start
    stuck.set()  # release the abandoned daemon thread
    assert out == {"fast": "quick"}  # slow abandoned, not awaited
    assert elapsed < 1.0  # returned at ~budget, not 5s


def test_run_judges_parallel_drops_raising_job():
    def boom():
        raise RuntimeError("judge blew up")

    out = run_judges_parallel({"boom": boom, "ok": lambda: 1}, budget=1.0)
    assert out == {"ok": 1}


def test_run_judges_parallel_empty_is_noop():
    assert run_judges_parallel({}, budget=1.0) == {}


# --- run_posttool_judges ----------------------------------------------------


def test_run_posttool_judges_packs_results():
    res = run_posttool_judges(
        reconcile=lambda: [{"action": "retract", "id": "T1"}],
        discover=lambda: [{"title": "F", "check": "pytest"}],
        disarm=lambda: "breaker open",
        hint=lambda: "try X",
        budget=2.0,
    )
    assert res.reconcile_actions == [{"action": "retract", "id": "T1"}]
    assert res.frontier_additions == [{"title": "F", "check": "pytest"}]
    assert res.disarm_message == "breaker open"
    assert res.hint_text == "try X"
    assert set(res.completed) == {"reconcile", "discover", "disarm", "hint"}


def test_run_posttool_judges_coerces_non_list_deltas():
    res = run_posttool_judges(reconcile=lambda: None, discover=lambda: "bad", budget=1.0)
    assert res.reconcile_actions == []
    assert res.frontier_additions == []


def test_run_posttool_judges_skips_unset_jobs():
    res = run_posttool_judges(disarm=lambda: "msg", budget=1.0)
    assert res.disarm_message == "msg"
    assert res.reconcile_actions == []
    assert res.hint_text == ""
    assert set(res.completed) == {"disarm"}


# --- claim_spec_judging coalesce -------------------------------------------


def test_claim_spec_judging_coalesces_within_window():
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        transcript = Path(cwd) / "t.jsonl"
        _write_transcript(transcript, "fix the bug")
        payload = _payload_with_transcript(cwd, "claim", transcript)
        # First sibling claims; a second within the window is coalesced away.
        assert claim_spec_judging(payload, now=1000.0, window=2.0) is True
        assert claim_spec_judging(payload, now=1000.5, window=2.0) is False
        # Genuinely later sequential evidence (> window) re-claims.
        assert claim_spec_judging(payload, now=1003.0, window=2.0) is True


def test_claim_spec_judging_fails_open_without_transcript():
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        payload = {"session_id": "no-tx", "cwd": cwd}
        assert claim_spec_judging(payload, now=1000.0, window=2.0) is True
        assert claim_spec_judging(payload, now=1000.5, window=2.0) is True


def test_claim_spec_judging_fails_open_unreadable_transcript():
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        payload = {"session_id": "bad-tx", "cwd": cwd, "transcript_path": str(Path(cwd) / "missing.jsonl")}
        assert claim_spec_judging(payload, now=1000.0, window=2.0) is True
        assert claim_spec_judging(payload, now=1000.5, window=2.0) is True


def test_claim_spec_judging_identical_prompt_new_turn_not_suppressed():
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        transcript = Path(cwd) / "t.jsonl"
        _write_transcript(transcript, "same ask")
        payload_turn1 = _payload_with_transcript(cwd, "epoch", transcript)
        assert claim_spec_judging(payload_turn1, now=1000.0, window=5.0) is True
        assert claim_spec_judging(payload_turn1, now=1000.5, window=5.0) is False
        # Same prompt text again but transcript grew -> distinct epoch -> run again.
        _write_transcript(transcript, "same ask", "same ask")
        payload_turn2 = _payload_with_transcript(cwd, "epoch", transcript)
        assert claim_spec_judging(payload_turn2, now=1001.0, window=5.0) is True


def test_claim_spec_judging_concurrent_single_winner():
    # The real race: many siblings of one parallel tool batch claim at once. The
    # atomic DB compare-and-set must grant exactly one, regardless of interleaving --
    # a barrier maximizes contention so they all hit the claim simultaneously.
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        transcript = Path(cwd) / "t.jsonl"
        _write_transcript(transcript, "parallel batch")
        payload = _payload_with_transcript(cwd, "race", transcript)
        n = 8
        barrier = threading.Barrier(n)
        results: list[bool] = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            granted = claim_spec_judging(payload, window=5.0)
            with lock:
                results.append(granted)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == n
        assert results.count(True) == 1  # exactly one claimant across the batch


def test_run_judge_fanout_bounded_when_orchestrator_fails(monkeypatch):
    # If the orchestrator import/call fails, the hook must NOT fall back to four
    # straight-line ~90s judges (that would blow the host timeout). It returns empty
    # context and never invokes the compute thunks sequentially.
    import gate_post_tool
    import posttool_judges
    import spec_judge

    calls = {"n": 0}

    def tripwire(*_a, **_k):
        calls["n"] += 1
        return []

    monkeypatch.setattr(spec_judge, "compute_reconcile_actions", tripwire)
    monkeypatch.setattr(spec_judge, "compute_frontier_additions", tripwire)

    def boom(*_a, **_k):
        raise RuntimeError("daemon down")

    monkeypatch.setattr(posttool_judges, "run_posttool_judges", boom)

    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        spec = spec_template()
        spec["requires_tasks"] = True
        spec["restated_goal"] = "g"
        spec["tasks"] = [{"id": "T1", "title": "t", "check": "rg x", "status": "failed", "added_by": "agent"}]
        save_spec(cwd, "sess", spec)
        payload = {
            "tool_name": "Read",
            "tool_input": {"path": "hooks/gate_post_tool.py"},
            "tool_response": {"success": True},
            "session_id": "sess",
            "cwd": cwd,
        }
        discovery, breaker, hint = gate_post_tool._run_judge_fanout(
            payload,
            {"read_paths": ["hooks/gate_post_tool.py"]},
            cwd,
            "Read",
            "",
            True,
            reads=["hooks/gate_post_tool.py"],
            fetched=[],
            ran=[],
            mcp_ev=[],
            research_ev=[],
            cmd_out=[],
            verification=None,
            repeat_count=0,
        )
        assert (discovery, breaker, hint) == ("", "", "")
        assert calls["n"] == 0  # compute thunks never run sequentially on fallback


def test_apply_frontier_additions_dedups_titles():
    from spec import append_frontier_task, spec_template
    from spec_judge import apply_frontier_additions

    spec = spec_template()
    spec["heavy_workflow"] = True
    append_frontier_task(spec, "Existing approach", "pytest", added_by="judge")
    candidates = [
        {"title": "Existing approach", "check": "x", "scope_paths": [], "reason": ""},  # dup of existing frontier
        {"title": "New one", "check": "y", "scope_paths": [], "reason": ""},
        {"title": "new ONE", "check": "z", "scope_paths": [], "reason": ""},  # spacing/case dup
        {"title": "New one (extra detail)", "check": "w", "scope_paths": [], "reason": ""},  # trailing paren dup
    ]
    added = apply_frontier_additions(spec, candidates)
    assert [t["title"] for t in added] == ["New one"]


def test_frontier_research_counter_reaches_threshold_concurrently():
    with tempfile.TemporaryDirectory() as data_dir:
        os.environ["UNIFABLE_DATA"] = data_dir
        skey = "frontier-race"
        # Bootstrap schema once; parallel connect()+DDL from cold start can race SQLite.
        with db.connect() as conn:
            assert conn is not None
        n = 3
        barrier = threading.Barrier(n)
        bumped: list[int] = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            n_tools, _disc = db.frontier_bump_research(skey)
            with lock:
                bumped.append(n_tools)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(bumped) == [1, 2, 3]
        assert db.frontier_get_counts(skey) == (3, 0)
        assert max(bumped) >= 3


def test_plan_discover_job_hits_threshold_after_three_research_tools(monkeypatch):
    import evidence_policy
    import gate_post_tool

    monkeypatch.setattr(evidence_policy, "resolve_grade", lambda *_a, **_k: "HEAVY")

    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        spec = spec_template()
        spec["heavy_workflow"] = True
        payload = {"session_id": "heavy", "cwd": cwd}
        ledger = {}
        from ledger import ledger_key

        skey = ledger_key(payload)
        want, _rec = gate_post_tool._plan_discover_job(payload, ledger, spec, "Read")
        assert want is False
        assert db.frontier_get_counts(skey)[0] == 1
        want, _rec = gate_post_tool._plan_discover_job(payload, ledger, spec, "Grep")
        assert want is False
        assert db.frontier_get_counts(skey)[0] == 2
        want, recorder = gate_post_tool._plan_discover_job(payload, ledger, spec, "Glob")
        assert want is True
        assert recorder is not None
        assert db.frontier_get_counts(skey)[0] == 3


def test_concurrent_hygiene_merge_survives():
    import gate_post_tool

    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        path_a = Path(cwd) / "a.py"
        path_b = Path(cwd) / "b.py"
        path_a.write_text("a\n", encoding="utf-8")
        path_b.write_text("b\n", encoding="utf-8")
        spec = spec_template()
        spec["requires_tasks"] = True
        spec["restated_goal"] = "g"
        save_spec(cwd, "sess", spec)

        barrier = threading.Barrier(2)

        def worker(read_path: str):
            barrier.wait()
            activity = {
                "read_paths": [read_path],
                "fetched_urls": [],
                "ran_commands": [],
                "tool_evidence": [],
                "command_outputs": [],
            }
            gate_post_tool._apply_hygiene_only(cwd, "sess", activity)

        t1 = threading.Thread(target=worker, args=(str(path_a),))
        t2 = threading.Thread(target=worker, args=(str(path_b),))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        merged = load_spec(cwd, "sess")
        cites = [str(item.get("cite") or "") for item in merged.get("repo_context") or []]
        assert any("a.py" in c for c in cites)
        assert any("b.py" in c for c in cites)


def test_hygiene_persists_when_spec_judging_coalesced(monkeypatch):
    import gate_post_tool
    import posttool_judges
    import spec_judge

    claims = iter([True, False])

    def fake_claim(_payload, **_kw):
        return next(claims)

    monkeypatch.setattr(posttool_judges, "claim_spec_judging", fake_claim)
    monkeypatch.setattr(spec_judge, "compute_reconcile_actions", lambda *_a, **_k: [])
    monkeypatch.setattr(posttool_judges, "run_posttool_judges", lambda **_kw: posttool_judges.PosttoolResult())

    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        read_path = Path(cwd) / "seen.py"
        read_path.write_text("x\n", encoding="utf-8")
        spec = spec_template()
        spec["requires_tasks"] = True
        spec["restated_goal"] = "g"
        save_spec(cwd, "sess", spec)
        payload = {
            "tool_name": "Read",
            "tool_input": {"path": str(read_path)},
            "tool_response": {"success": True},
            "session_id": "sess",
            "cwd": cwd,
        }
        ledger = {"read_paths": [str(read_path)]}
        for _ in range(2):
            gate_post_tool._run_judge_fanout(
                payload,
                ledger,
                cwd,
                "Read",
                "",
                True,
                reads=[str(read_path)],
                fetched=[],
                ran=[],
                mcp_ev=[],
                research_ev=[],
                cmd_out=[],
                verification=None,
                repeat_count=0,
            )
        merged = load_spec(cwd, "sess")
        cites = [str(item.get("cite") or "") for item in merged.get("repo_context") or []]
        assert any("seen.py" in c for c in cites)


# --- update_spec RMW --------------------------------------------------------


def test_update_spec_none_when_absent():
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        called = {"n": 0}

        def updater(_base):
            called["n"] += 1

        assert update_spec(cwd, "missing", updater) is None
        assert called["n"] == 0  # no base -> updater never runs


def test_update_spec_applies_and_saves():
    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        spec = spec_template()
        spec["restated_goal"] = "g"
        spec["tasks"] = [{"id": "T1", "title": "t", "check": "true", "status": "pending"}]
        save_spec(cwd, "sess", spec)

        def updater(base):
            base["restated_goal"] = "updated"

        merged = update_spec(cwd, "sess", updater)
        assert merged is not None and merged["restated_goal"] == "updated"
        assert load_spec(cwd, "sess")["restated_goal"] == "updated"


# --- delta-merge (posttool_background.run_reconcile_job) --------------------


def _base_spec(cwd: str) -> None:
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["heavy_workflow"] = True
    spec["restated_goal"] = "Remove the old route"
    spec["tasks"] = [
        {"id": "T1", "title": "Old route still exists", "check": "rg old-route src", "status": "failed", "added_by": "agent"}
    ]
    save_spec(cwd, "sess", spec)


def test_run_reconcile_job_merges_reconcile_then_frontier(monkeypatch):
    import posttool_background
    import spec_judge

    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        _base_spec(cwd)
        reconcile_actions = [
            {
                "action": "retract",
                "id": "T1",
                "reason": "old route is gone",
                "evidence_refs": ["rg old-route src -> 0 matches"],
            }
        ]
        frontier_additions = [
            {
                "title": "Zero-copy mmap",
                "check": "pytest tests/test_mmap.py -q",
                "scope_paths": ["src/parser.py"],
                "reason": "mmap",
            }
        ]
        monkeypatch.setattr(spec_judge, "compute_reconcile_actions", lambda *_a, **_k: reconcile_actions)
        monkeypatch.setattr(spec_judge, "compute_frontier_additions", lambda *_a, **_k: frontier_additions)
        # Feed the evidence the retract cites so apply_reconcile_actions accepts it.
        import db as _db
        from ledger import ledger_key

        skey = ledger_key({"session_id": "sess", "cwd": cwd})
        _db.activity_add(skey, "command_outputs", ["rg old-route src -> 0 matches; old route removed"])

        payload = {"session_id": "sess", "cwd": cwd}
        ctx = posttool_background.run_reconcile_job(payload, want_reconcile=True, want_discover=True)

        merged = load_spec(cwd, "sess")
        by_id = {t["id"]: t for t in merged["tasks"]}
        # Reconcile applied: T1 retracted.
        assert by_id["T1"]["status"] == "retracted"
        # Frontier appended with a fresh, non-colliding id (T2) after reconcile.
        assert "T2" in by_id
        assert by_id["T2"]["approach_kind"] == "frontier"
        assert by_id["T2"]["title"] == "Zero-copy mmap"
        # Combined context carries both the reconcile headline and the frontier board.
        assert "Judge retracted T1" in ctx
        assert "Judge added frontier approach(s): T2." in ctx
        assert "Explore ALL frontiers thoroughly" in ctx
        assert "Zero-copy mmap" in ctx
        # And the same context is enqueued for the next PreToolUse to drain.
        from spec_io import _spec_key, canonical_project_root

        drained = _db.posttool_bg_drain(_spec_key(canonical_project_root(cwd), "sess"))
        assert "Judge retracted T1" in drained
        assert "Zero-copy mmap" in drained


def test_run_reconcile_job_records_discovery_once(monkeypatch):
    import db as _db
    import posttool_background
    import spec_judge
    from ledger import ledger_key

    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        _base_spec(cwd)
        frontier_additions = [{"title": "F", "check": "pytest", "scope_paths": [], "reason": "r"}]
        monkeypatch.setattr(spec_judge, "compute_reconcile_actions", lambda *_a, **_k: [])
        monkeypatch.setattr(spec_judge, "compute_frontier_additions", lambda *_a, **_k: frontier_additions)
        payload = {"session_id": "sess", "cwd": cwd}
        skey = ledger_key(payload)
        posttool_background.run_reconcile_job(payload, want_reconcile=False, want_discover=True)
        assert _db.frontier_get_counts(skey)[1] == 1


def test_run_reconcile_job_empty_is_noop(monkeypatch):
    import posttool_background
    import spec_judge

    with tempfile.TemporaryDirectory() as data_dir, tempfile.TemporaryDirectory() as cwd:
        os.environ["UNIFABLE_DATA"] = data_dir
        _base_spec(cwd)
        monkeypatch.setattr(spec_judge, "compute_reconcile_actions", lambda *_a, **_k: [])
        monkeypatch.setattr(spec_judge, "compute_frontier_additions", lambda *_a, **_k: [])
        payload = {"session_id": "sess", "cwd": cwd}
        assert posttool_background.run_reconcile_job(payload, want_reconcile=True, want_discover=True) == ""
