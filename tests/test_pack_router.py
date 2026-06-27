#!/usr/bin/env python3
"""Unit tests for scripts/gate/pack_router.py."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import pack_router  # noqa: E402
from ledger import load_ledger  # noqa: E402


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
        ("delegate this to a subagent worker", ["subagent-brief"]),
        ("orchestrate this in parallel", []),
        (
            "debug and implement and design and render and delegate all at once",
            ["investigation", "grounding", "decision-trace", "domain-verify", "subagent-brief"],
        ),
        ("analyze the sources and claims in this summary", []),
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
    assert ctx.startswith("[investigation]")
    assert "Reproduce or read the actual failure output" in ctx
    assert "investigation-protocol.txt" not in ctx
    assert "/plugin/root/packs/" not in ctx


def test_format_context_inline_multi(routes: list[pack_router.PackRoute]) -> None:
    matched = pack_router.match_routes("debug html implement subagent", routes)
    ctx = pack_router.format_context(matched, packs_root="${CLAUDE_PLUGIN_ROOT}")
    assert "[investigation]" in ctx
    assert "[grounding]" in ctx
    assert "[domain-verify]" in ctx
    assert "[subagent-brief]" in ctx
    assert all(f"[{r.tag}]" in ctx for r in matched)
    assert "explicit delegation/spawn requests" in ctx
    assert "failing/error-path check" in ctx


def test_route_prompt_returns_envelope(routes: list[pack_router.PackRoute]) -> None:
    out = pack_router.route_prompt("debug failing test", root=REPO)
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert "[investigation]" in hso["additionalContext"]
    assert "Reproduce or read the actual failure output" in hso["additionalContext"]


def test_route_prompt_empty_when_no_match() -> None:
    assert pack_router.route_prompt("hello there", root=REPO) is None


def test_route_prompt_ignores_pasted_corpus() -> None:
    """The router must match the operative instruction, not pasted corpus.

    A prompt that pastes a large body full of pack keywords (render, subagent,
    design, debug) and ends with an unrelated instruction must NOT fire the packs
    keyed off the paste -- otherwise pasting a hook dump injects every pack."""
    corpus = (
        "render an svg chart on the canvas for the website\n"
        "delegate this to a subagent and orchestrate in parallel\n"
        "design the architecture and choose an approach\n"
        "debug this failing crash traceback\n"
    ) * 40
    # The corpus alone is full of routing signal ...
    assert pack_router.match_routes(corpus, pack_router.load_manifest(REPO))
    # ... but the operative instruction after the user-turn marker has none.
    prompt = corpus + "\n> what is the capital of France"
    assert pack_router.route_prompt(prompt, root=REPO) is None


def test_route_prompt_caps_packs_with_suppression_marker() -> None:
    """When more than the cap match, emit the cap and disclose the suppression."""
    prompt = "debug and implement and design and render and delegate all at once"
    out = pack_router.route_prompt(prompt, root=REPO)
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    matched = pack_router.match_routes(prompt, pack_router.load_manifest(REPO))[: pack_router._MAX_PACKS]
    assert all(f"[{r.tag}]" in ctx for r in matched)
    assert "suppress" in ctx.lower()


def test_route_prompt_subagent_brief_requires_explicit_delegation_wording() -> None:
    assert pack_router.route_prompt("parallelize this investigation", root=REPO) is None
    out = pack_router.route_prompt("spawn a worker for this investigation", root=REPO)
    assert out is not None
    assert "[subagent-brief]" in out["hookSpecificOutput"]["additionalContext"]


def test_route_prompt_suppresses_previously_fired_tags_for_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    payload = {"session_id": "router-once", "cwd": str(REPO)}

    first = pack_router.route_prompt("debug failing test", root=REPO, input_data=payload)
    assert first is not None
    assert "[investigation]" in first["hookSpecificOutput"]["additionalContext"]

    second = pack_router.route_prompt("debug failing test again", root=REPO, input_data=payload)
    assert second is None

    third = pack_router.route_prompt("debug and implement the pipeline", root=REPO, input_data=payload)
    assert third is not None
    ctx = third["hookSpecificOutput"]["additionalContext"]
    assert "[investigation]" not in ctx
    assert "[domain-verify]" in ctx

    ledger = load_ledger(payload)
    assert ledger["router_matched_tags"] == ["domain-verify"]
    assert ledger["router_fired_tags"] == ["investigation", "domain-verify"]


def test_route_prompt_dedup_is_per_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    first_session = {"session_id": "router-session-a", "cwd": str(REPO)}
    second_session = {"session_id": "router-session-b", "cwd": str(REPO)}

    assert pack_router.route_prompt("debug failing test", root=REPO, input_data=first_session) is not None
    assert pack_router.route_prompt("debug failing test", root=REPO, input_data=first_session) is None
    assert pack_router.route_prompt("debug failing test", root=REPO, input_data=second_session) is not None


def test_main_fail_open_on_bad_manifest(tmp_path: Path, monkeypatch, capsys) -> None:
    bad_root = tmp_path / "plugin"
    (bad_root / "packs").mkdir(parents=True)
    (bad_root / "packs" / "router-manifest.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(pack_router, "_plugin_root", lambda: bad_root)
    monkeypatch.setattr(pack_router, "read_stdin_json", lambda: {"prompt": "debug bug"})
    pack_router.main()
    assert capsys.readouterr().out == ""


def test_router_sh_integration(tmp_path: Path) -> None:
    import subprocess

    router = REPO / "hooks" / "router.sh"
    payload = json.dumps({"prompt": "debug and implement subagent", "session_id": "router-sh", "cwd": str(REPO)})
    env = {"CLAUDE_PLUGIN_ROOT": str(REPO), "UNIFABLE_DATA": str(tmp_path), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    proc = subprocess.run(
        ["bash", str(router)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    obj = json.loads(proc.stdout.strip())
    ctx = obj["hookSpecificOutput"]["additionalContext"]
    assert "[investigation]" in ctx
    assert "[domain-verify]" in ctx
    assert "[subagent-brief]" in ctx
    assert "Reproduce or read the actual failure output" in ctx
    assert "SOFTWARE:" in ctx
    assert "Objective" in ctx

    repeated = subprocess.run(
        ["bash", str(router)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env=env,
        check=False,
    )
    assert repeated.returncode == 0
    assert repeated.stdout.strip() == ""
