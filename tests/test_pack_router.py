#!/usr/bin/env python3
"""Unit tests for scripts/gate/pack_router.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import pack_router  # noqa: E402


@pytest.fixture
def routes() -> list[pack_router.PackRoute]:
    return pack_router.load_manifest(REPO)


def test_load_manifest_has_five_routes(routes: list[pack_router.PackRoute]) -> None:
    assert len(routes) == 5
    tags = {r.tag for r in routes}
    assert tags == {"investigation", "grounding", "decision-trace", "domain-verify", "subagent-brief"}


def test_routes_have_body(routes: list[pack_router.PackRoute]) -> None:
    for r in routes:
        assert r.body, f"route {r.tag} has empty body"
        assert len(r.body) > 50, f"route {r.tag} body too short"


@pytest.mark.parametrize(
    ("prompt", "expected_tags"),
    [
        ("debug this failing test", ["investigation"]),
        ("implement the judge feature and build the pipeline", ["domain-verify"]),
        ("design the architecture and choose an approach", ["decision-trace"]),
        ("render an svg chart on the canvas for the website", ["grounding"]),
        ("delegate this to a subagent and orchestrate in parallel", ["subagent-brief"]),
        (
            "debug and implement and design and render and delegate all at once",
            ["investigation", "grounding", "decision-trace", "domain-verify", "subagent-brief"],
        ),
        ("plain greeting with no routing signal", []),
        ("", []),
    ],
)
def test_match_routes(
    routes: list[pack_router.PackRoute],
    prompt: str,
    expected_tags: list[str],
) -> None:
    matched = pack_router.match_routes(prompt, routes)
    assert [r.tag for r in matched] == expected_tags


def test_format_context_inline_single(routes: list[pack_router.PackRoute]) -> None:
    matched = pack_router.match_routes("debug the bug", routes)
    ctx = pack_router.format_context(matched, packs_root="/plugin/root")
    assert ctx.startswith("[unifable:investigation]")
    assert "Investigation protocol" in ctx
    assert "investigation-protocol.txt" not in ctx
    assert "/plugin/root/packs/" not in ctx


def test_format_context_inline_multi(routes: list[pack_router.PackRoute]) -> None:
    matched = pack_router.match_routes("debug html implement subagent", routes)
    ctx = pack_router.format_context(matched, packs_root="${CLAUDE_PLUGIN_ROOT}")
    assert "[unifable:investigation]" in ctx
    assert "[unifable:grounding]" in ctx
    assert "[unifable:domain-verify]" in ctx
    assert "[unifable:subagent-brief]" in ctx
    assert ctx.count("[unifable:") == len(matched)


def test_route_prompt_returns_envelope(routes: list[pack_router.PackRoute]) -> None:
    out = pack_router.route_prompt("debug failing test", root=REPO)
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert "[unifable:investigation]" in hso["additionalContext"]
    assert "Investigation protocol" in hso["additionalContext"]


def test_route_prompt_empty_when_no_match() -> None:
    assert pack_router.route_prompt("hello there", root=REPO) is None


def test_main_fail_open_on_bad_manifest(tmp_path: Path, monkeypatch, capsys) -> None:
    bad_root = tmp_path / "plugin"
    (bad_root / "packs").mkdir(parents=True)
    (bad_root / "packs" / "router-manifest.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(pack_router, "_plugin_root", lambda: bad_root)
    monkeypatch.setattr(pack_router, "read_stdin_json", lambda: {"prompt": "debug bug"})
    pack_router.main()
    assert capsys.readouterr().out == ""


def test_router_sh_integration() -> None:
    import subprocess

    router = REPO / "hooks" / "router.sh"
    payload = json.dumps({"prompt": "debug and implement subagent"})
    proc = subprocess.run(
        ["bash", str(router)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env={"CLAUDE_PLUGIN_ROOT": str(REPO)},
        check=False,
    )
    assert proc.returncode == 0
    obj = json.loads(proc.stdout.strip())
    ctx = obj["hookSpecificOutput"]["additionalContext"]
    assert "[unifable:investigation]" in ctx
    assert "[unifable:domain-verify]" in ctx
    assert "[unifable:subagent-brief]" in ctx
    assert "Investigation protocol" in ctx
    assert "Domain verification recipes" in ctx
    assert "Subagent brief template" in ctx
