import importlib.util
import json
import shutil
import sys
from pathlib import Path


def _load_module(filename, modname):
    path = Path(__file__).resolve().parents[1] / "benchmark" / filename
    spec = importlib.util.spec_from_file_location(modname, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[modname] = module  # let dataclasses resolve cls.__module__
    spec.loader.exec_module(module)
    return module


def _load_summarize():
    return _load_module("summarize.py", "benchmark_summarize")


def test_files_changed_counts_distinct_paths():
    bench = _load_module("bench.py", "benchmark_bench")
    # Two edits to the same file count once; a second file counts again.
    claude_stream = "\n".join(
        [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "a.py"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "b.py"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}),
        ]
    )
    assert bench._files_changed_from_text(claude_stream, "claude") == 2

    codex_stream = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"item_type": "file_change", "path": "a.py"}}),
            json.dumps({"type": "item.completed", "item": {"item_type": "command_execution", "command": "ls"}}),
        ]
    )
    assert bench._files_changed_from_text(codex_stream, "codex") == 1


def _write_session(raw_dir, name, host, unifable, elapsed, tokens):
    path = raw_dir / name
    path.mkdir(parents=True)
    (path / "meta.json").write_text(
        json.dumps(
            {
                "host": host,
                "model": "opus-4.8" if host == "claude" else "gpt-5.5",
                "effort": "xhigh",
                "unifable": unifable,
                "status": "completed",
            }
        ),
        encoding="utf-8",
    )
    (path / "timing.json").write_text(json.dumps({"elapsed_seconds": elapsed}), encoding="utf-8")
    (path / "usage.json").write_text(json.dumps({"total_tokens": tokens}), encoding="utf-8")


def test_summary_requires_all_four_benchmark_cells(tmp_path):
    summarize = _load_summarize()
    raw = tmp_path / "raw"
    _write_session(raw, "claude-unifable", "claude", True, 1.0, 100)
    _write_session(raw, "claude-baseline", "claude", False, 2.0, 200)
    _write_session(raw, "codex-unifable", "codex", True, 3.0, 300)
    _write_session(raw, "codex-baseline", "codex", False, 4.0, 400)

    summary = summarize.summarize(raw)

    assert {row["condition"] for row in summary["aggregates"]} == {
        "claude:unifable",
        "claude:baseline",
        "codex:unifable",
        "codex:baseline",
    }
    assert all(row["runs"] == 1 for row in summary["aggregates"])
    assert summarize.is_accepted(summary)

    shutil.rmtree(raw / "codex-baseline")
    incomplete = summarize.summarize(raw)

    assert not summarize.is_accepted(incomplete)
    assert summarize.missing_conditions(incomplete) == {"codex:baseline"}


def _write_session_full(raw_dir, name, host, unifable, *, elapsed, usage, files_changed, model=None):
    path = raw_dir / name
    path.mkdir(parents=True)
    (path / "meta.json").write_text(
        json.dumps(
            {
                "host": host,
                "model": model or ("opus-4.8" if host == "claude" else "gpt-5.5"),
                "effort": "xhigh",
                "unifable": unifable,
                "status": "completed",
                "files_changed": files_changed,
            }
        ),
        encoding="utf-8",
    )
    (path / "timing.json").write_text(json.dumps({"elapsed_seconds": elapsed}), encoding="utf-8")
    (path / "usage.json").write_text(json.dumps(usage), encoding="utf-8")


def test_cost_weighting_separates_cache_from_fresh(tmp_path):
    summarize = _load_summarize()
    raw = tmp_path / "raw"
    # Anthropic reports input_tokens net of cache; cache read/write are separate.
    _write_session_full(
        raw,
        "claude-unifable",
        "claude",
        True,
        elapsed=1.0,
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 1000,
            "cached_tokens": 1000,
            "cache_creation_input_tokens": 200,
            "total_tokens": 1350,
        },
        files_changed=2,
    )
    # OpenAI/Codex reports input_tokens inclusive of cached tokens.
    _write_session_full(
        raw,
        "codex-unifable",
        "codex",
        True,
        elapsed=2.0,
        usage={
            "input_tokens": 1000,
            "output_tokens": 100,
            "cached_tokens": 800,
            "reasoning_output_tokens": 20,
            "total_tokens": 1120,
        },
        files_changed=1,
    )

    summary = summarize.summarize(raw)
    by_name = {s["session"]: s for s in summary["sessions"]}

    claude = by_name["claude-unifable"]
    assert claude["fresh_input_tokens"] == 100
    assert claude["cached_input_tokens"] == 1000
    assert claude["cache_write_tokens"] == 200
    # (100*5 + 1000*0.5 + 200*6.25 + 50*25) / 1e6
    assert claude["est_cost_usd"] == 0.0035
    assert claude["files_changed"] == 2

    codex = by_name["codex-unifable"]
    assert codex["fresh_input_tokens"] == 200  # 1000 input - 800 cached
    assert codex["cached_input_tokens"] == 800
    # (200*5 + 800*0.5 + (100+20)*30) / 1e6
    assert codex["est_cost_usd"] == 0.005

    assert summary["pricing_as_of"]
    agg = {row["condition"]: row for row in summary["aggregates"]}
    assert agg["claude:unifable"]["mean_est_cost_usd"] == 0.0035
    assert agg["codex:unifable"]["mean_files_changed"] == 1.0


def test_failed_cells_excluded_from_means(tmp_path):
    summarize = _load_summarize()
    raw = tmp_path / "raw"
    # One good repeat and one transient failure (529-style) for the same condition.
    _write_session_full(
        raw,
        "claude-unifable-r1",
        "claude",
        True,
        elapsed=200.0,
        usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        files_changed=1,
    )
    bad = raw / "claude-unifable-r2"
    bad.mkdir(parents=True)
    (bad / "meta.json").write_text(
        json.dumps({"host": "claude", "model": "opus-4.8", "unifable": True, "status": "failed", "files_changed": 0}),
        encoding="utf-8",
    )
    (bad / "timing.json").write_text(json.dumps({"elapsed_seconds": 5.0}), encoding="utf-8")
    (bad / "usage.json").write_text(json.dumps({"total_tokens": 0}), encoding="utf-8")

    summary = summarize.summarize(raw)
    agg = {row["condition"]: row for row in summary["aggregates"]}["claude:unifable"]
    assert agg["runs"] == 2
    assert agg["completed_runs"] == 1
    # Mean reflects only the completed cell, not the failure.
    assert agg["mean_elapsed_seconds"] == 200.0
    assert agg["mean_files_changed"] == 1.0


def test_est_cost_is_none_for_unknown_model(tmp_path):
    summarize = _load_summarize()
    raw = tmp_path / "raw"
    _write_session_full(
        raw,
        "claude-unifable",
        "claude",
        True,
        elapsed=1.0,
        usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        files_changed=0,
        model="mystery-model",
    )
    summary = summarize.summarize(raw)
    assert summary["sessions"][0]["est_cost_usd"] is None
