#!/usr/bin/env python3
"""Tests for generated hook-output and judge-prompt docs."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "scripts" / "generate_docs.py"

spec = importlib.util.spec_from_file_location("generate_docs", GENERATOR_PATH)
assert spec and spec.loader
generate_docs = importlib.util.module_from_spec(spec)
sys.modules["generate_docs"] = generate_docs
spec.loader.exec_module(generate_docs)


def test_registered_hooks_are_listed_in_hook_docs():
    for host in ("claude", "codex"):
        doc = generate_docs.render_hook_doc(host)
        for hook in generate_docs.collect_hook_specs(host):
            assert hook.event in doc
            assert generate_docs._hook_command_name(hook.command) in doc


def test_judge_prompt_capture_covers_known_schema_names():
    prompts = generate_docs.collect_judge_prompts()
    schema_names = {case.schema_name for case in prompts}

    assert {
        "grade_classify",
        "judge_heal",
        "validate_all",
        "task_verdict",
        "frontier_discover",
        "dispute_verdict",
        "hint",
        "groundedness",
        "loop_release",
        "goal_stop",
        "completion_handoff",
        "frontier_comparison",
    } <= schema_names
    assert all(case.system for case in prompts)
    assert all(case.user for case in prompts)
    assert all("session.update" in case.transport for case in prompts)


def test_stop_payload_rendering_tracks_host_visible_difference():
    claude = generate_docs._sample_stop_payload("claude")
    codex = generate_docs._sample_stop_payload("codex")

    assert "hookSpecificOutput" in claude
    assert "additionalContext" in claude["hookSpecificOutput"]
    assert "hookSpecificOutput" not in codex
    assert "Action required:" in codex["reason"]


def test_all_docs_render_deterministically():
    first = generate_docs.render_all_docs()
    second = generate_docs.render_all_docs()

    assert first == second
    assert set(first) == {
        "claude-hookoutputs.md",
        "codex-hookoutputs.md",
        "judgeprompts.md",
    }
    assert "# Judge Prompts" in first["judgeprompts.md"]
    assert all(str(ROOT) not in text for text in first.values())


def test_router_fixture_renders_matched_pack_context():
    """The UserPromptSubmit router fixture must not be empty -- the prompt is
    chosen to match every route in packs/router-manifest.json."""
    out = generate_docs._run_router_fixture()

    assert "hookSpecificOutput" in out
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert isinstance(ctx, str)
    assert ctx.strip(), "router fixture produced empty additionalContext"
    for tag in (
        "[investigation]",
        "[grounding]",
        "[decision-trace]",
        "[domain-verify]",
        "[subagent-brief]",
    ):
        assert tag in ctx, f"router fixture missing pack tag {tag}"


def test_generated_hook_docs_contain_router_pack_context():
    for host in ("claude", "codex"):
        doc = generate_docs.render_hook_doc(host)
        assert "[investigation]" in doc, f"{host} hook doc missing router pack context (fixture returned empty)"


def test_generated_docs_write_and_check_round_trip(tmp_path):
    written = generate_docs.write_docs(tmp_path)
    ok, problems = generate_docs.check_docs(tmp_path)

    assert ok
    assert problems == []
    assert {path.name for path in written} == {
        "claude-hookoutputs.md",
        "codex-hookoutputs.md",
        "judgeprompts.md",
    }

    (tmp_path / "judgeprompts.md").write_text("stale\n", encoding="utf-8")
    ok, problems = generate_docs.check_docs(tmp_path)
    assert not ok
    assert any("judgeprompts.md" in problem for problem in problems)
