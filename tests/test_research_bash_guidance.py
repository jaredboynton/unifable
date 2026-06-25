#!/usr/bin/env python3
"""Tests for install-detected explore skill guidance copy."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "gate"
sys.path.insert(0, str(GATE))

import research_bash_guidance as rbg  # noqa: E402


def _clear_cache() -> None:
    rbg.clear_explore_guidance_cache()


def _write_explore_skill(root: Path, *, include_websearch: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text("---\nname: explore\n---\n", encoding="utf-8")
    trace = root / "scripts" / "trace.sh"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    if include_websearch:
        (root / "scripts" / "websearch.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return trace.resolve()


def test_resolve_absent_when_no_skill_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_EXPLORE_SKILL_ROOT", raising=False)
    assert rbg.resolve_explore_trace_sh() is None
    assert rbg.resolve_explore_websearch_sh() is None
    summary = rbg.bash_allowed_summary()
    assert "trace.sh" not in summary
    assert "websearch.sh" not in summary
    assert "explore" not in summary.lower()
    assert "unifusion scripts" in summary


def test_resolve_finds_agents_skill_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_EXPLORE_SKILL_ROOT", raising=False)
    trace = _write_explore_skill(tmp_path / ".agents" / "skills" / "explore")
    assert rbg.resolve_explore_trace_sh() == trace
    websearch = tmp_path / ".agents" / "skills" / "explore" / "scripts" / "websearch.sh"
    assert rbg.resolve_explore_websearch_sh() == websearch.resolve()
    summary = rbg.bash_allowed_summary()
    assert "explore trace.sh/websearch.sh" in summary
    detail = rbg.allowed_research_bash_detail()
    assert "~/.agents/skills/explore/scripts/trace.sh" in detail
    assert "~/.agents/skills/explore/scripts/websearch.sh" in detail


def test_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    custom = tmp_path / "custom-explore"
    trace = _write_explore_skill(custom)
    monkeypatch.setenv("UNIFABLE_EXPLORE_SKILL_ROOT", str(custom))
    assert rbg.resolve_explore_trace_sh() == trace


def test_invalid_skill_md_name_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    root = tmp_path / ".agents" / "skills" / "explore"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: other\n---\n", encoding="utf-8")
    trace = root / "scripts" / "trace.sh"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_EXPLORE_SKILL_ROOT", raising=False)
    assert rbg.resolve_explore_trace_sh() is None


def test_trace_only_without_websearch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_EXPLORE_SKILL_ROOT", raising=False)
    _write_explore_skill(tmp_path / ".agents" / "skills" / "explore", include_websearch=False)
    _clear_cache()
    assert rbg.resolve_explore_trace_sh() is not None
    assert rbg.resolve_explore_websearch_sh() is None
    summary = rbg.bash_allowed_summary()
    assert "trace.sh" in summary
    assert "websearch.sh" not in summary


def test_list_item_comma_hygiene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_EXPLORE_SKILL_ROOT", raising=False)
    assert rbg.explore_trace_list_item() == ""
    detail = rbg.allowed_research_bash_detail()
    assert ", ," not in detail
    assert ", the unifusion skill scripts" in detail

    _write_explore_skill(tmp_path / ".agents" / "skills" / "explore")
    _clear_cache()
    item = rbg.explore_trace_list_item()
    assert item.startswith(", the explore skill's trace.sh (")
    assert " and websearch.sh (" in item
    detail = rbg.allowed_research_bash_detail()
    assert ", ," not in detail
    assert ", the explore skill's trace.sh" in detail
    assert "websearch.sh" in detail


def test_markdown_placeholders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_EXPLORE_SKILL_ROOT", raising=False)
    assert rbg.explore_trace_list_item_md() == ""
    assert rbg.explore_trace_inline_md() == ""

    _write_explore_skill(tmp_path / ".agents" / "skills" / "explore")
    _clear_cache()
    assert "`trace.sh` (`~/.agents/skills/explore/scripts/trace.sh`)" in rbg.explore_trace_list_item_md()
    assert "`websearch.sh` (`~/.agents/skills/explore/scripts/websearch.sh`)" in rbg.explore_trace_list_item_md()
    assert rbg.explore_trace_inline_md().endswith(", ")
