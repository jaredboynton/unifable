#!/usr/bin/env python3
"""Tests for install-detected unitrace skill guidance copy."""

from __future__ import annotations

import runpy
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
    (root / "SKILL.md").write_text("---\nname: unitrace\n---\n", encoding="utf-8")
    unitrace = root / "scripts" / "unitrace.sh"
    unitrace.parent.mkdir(parents=True, exist_ok=True)
    unitrace.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    if include_websearch:
        (root / "scripts" / "websearch.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return unitrace.resolve()


def test_resolve_absent_when_no_skill_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_UNITRACE_SKILL_ROOT", raising=False)
    assert rbg.resolve_explore_trace_sh() is None
    assert rbg.resolve_explore_websearch_sh() is None
    assert (
        rbg.bash_allowed_summary()
        == "cd, ls, glob, rg, grep, echo (sink pipes only), ast-grep/sg, cat/nl (file reads only), "
        "head, tail, wc, sort, uniq, jq, "
        "read-only git, git add/commit/push (no --force), read-only python/python3 -c, "
        "cse-sweep sweep.sh, "
        "unifusion scripts, unifable spec CLI"
    )


def test_resolve_finds_agents_skill_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_UNITRACE_SKILL_ROOT", raising=False)
    trace = _write_explore_skill(tmp_path / ".agents" / "skills" / "unitrace")
    assert rbg.resolve_explore_trace_sh() == trace
    websearch = tmp_path / ".agents" / "skills" / "unitrace" / "scripts" / "websearch.sh"
    assert rbg.resolve_explore_websearch_sh() == websearch.resolve()
    summary = rbg.bash_allowed_summary()
    assert "unitrace unitrace.sh/websearch.sh" in summary
    detail = rbg.allowed_research_bash_detail()
    assert "~/.agents/skills/unitrace/scripts/unitrace.sh" in detail
    assert "~/.agents/skills/unitrace/scripts/websearch.sh" in detail


def test_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    custom = tmp_path / "custom-explore"
    trace = _write_explore_skill(custom)
    monkeypatch.setenv("UNIFABLE_UNITRACE_SKILL_ROOT", str(custom))
    assert rbg.resolve_explore_trace_sh() == trace


def test_invalid_skill_md_name_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    root = tmp_path / ".agents" / "skills" / "unitrace"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: other\n---\n", encoding="utf-8")
    trace = root / "scripts" / "unitrace.sh"
    trace.parent.mkdir(parents=True, exist_ok=True)
    trace.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_UNITRACE_SKILL_ROOT", raising=False)
    assert rbg.resolve_explore_trace_sh() is None


def test_trace_only_without_websearch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_UNITRACE_SKILL_ROOT", raising=False)
    _write_explore_skill(tmp_path / ".agents" / "skills" / "unitrace", include_websearch=False)
    _clear_cache()
    assert rbg.resolve_explore_trace_sh() is not None
    assert rbg.resolve_explore_websearch_sh() is None
    assert "unitrace unitrace.sh" in rbg.bash_allowed_summary()


def test_cat_nl_guidance_reaches_breaker_prompt_summary() -> None:
    prompts = runpy.run_path(str(REPO / "scripts" / "gate" / "breaker_prompts.py"))
    texts = {
        "summary": rbg.bash_allowed_summary(),
        "detail": rbg.allowed_research_bash_detail(),
        "breaker_summary": prompts["_research_bash_whitelist_summary"](),
    }
    failures: list[str] = []
    for name, text in texts.items():
        missing = [cmd for cmd in ("cat", "nl") if cmd not in text]
        if missing:
            failures.append(f"missing cat/nl in {name}: missing={missing}; text={text!r}")
        if "file reads only" not in text:
            failures.append(f"missing file-read constraint in {name}: {text!r}")
    assert not failures, "\n".join(failures)


def test_list_item_comma_hygiene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_UNITRACE_SKILL_ROOT", raising=False)
    assert rbg.explore_trace_list_item() == ""
    detail = rbg.allowed_research_bash_detail()
    assert "read-only python/python3 -c inspection" in detail
    assert "cat/nl file reads only" in detail
    assert "no writes, process spawn, or network" in detail
    assert ", ," not in detail
    assert ", the unifusion skill scripts" in detail
    assert "sweep.sh" in detail
    assert "sweep.sh" in rbg.bash_allowed_summary()
    _write_explore_skill(tmp_path / ".agents" / "skills" / "unitrace")
    _clear_cache()
    item = rbg.explore_trace_list_item()
    assert item.startswith(", the unitrace skill's unitrace.sh (")
    assert " and websearch.sh (" in item
    detail = rbg.allowed_research_bash_detail()
    assert ", ," not in detail
    assert ", the unitrace skill's unitrace.sh" in detail
    assert "websearch.sh" in detail


def test_markdown_placeholders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_cache()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNIFABLE_UNITRACE_SKILL_ROOT", raising=False)
    assert rbg.explore_trace_list_item_md() == ""
    assert rbg.explore_trace_inline_md() == ""

    _write_explore_skill(tmp_path / ".agents" / "skills" / "unitrace")
    _clear_cache()
    assert (
        rbg.explore_trace_list_item_md()
        == ", the unitrace skill's `unitrace.sh` (`~/.agents/skills/unitrace/scripts/unitrace.sh`) "
        "and `websearch.sh` (`~/.agents/skills/unitrace/scripts/websearch.sh`)"
    )
    assert (
        rbg.explore_trace_inline_md()
        == "the unitrace skill's `unitrace.sh` (`~/.agents/skills/unitrace/scripts/unitrace.sh`) "
        "and `websearch.sh` (`~/.agents/skills/unitrace/scripts/websearch.sh`), "
    )
