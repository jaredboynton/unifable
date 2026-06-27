#!/usr/bin/env python3
"""submit_enhance.py -- repo-grounded prompt-enhance gate policy.

Unit-tests the Python policy layer ONLY (no Node, no daemon, no network):
  - fire heuristic: under-specified (no path/file token, >= 20 words, not
    operational) and the UNIFABLE_PROMPT_ENHANCE=0 kill switch.
  - post-grade use gate: evidence_profile == "code" and mode in {normal, deep}.
  - hard gates: zero repo-specific commands (reject -> None), char cap 1200,
    cited_ranges clamped to 8 and typed.
  - fail-open: missing node / missing script / timeout / nonzero exit / bad
    JSON / ok:false / empty prompt all -> None (static baseline used).
  - entrypoint path resolves to the shipped skills/explore/scripts/enhance-prompt.mjs.

The Node entrypoint + live daemon are exercised separately (smoke + eval); here
the subprocess is stubbed so the suite stays fast, deterministic, and offline.

Run: python3 -m pytest tests/test_submit_enhance.py -q
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import submit_enhance as se  # noqa: E402

VAGUE_CODE = (
    "something is off with how the user prompt submit hook assembles the mode "
    "context the model keeps getting weird weak verification guidance and the "
    "mode lines read like conditionals diagnose and fix it"
)
PATHED = "fix the off-by-one in lib/pagination.ts slice helper around the cursor clamp"
OPERATIONAL = "draft a renewal reply to the account owner about the executive sponsor escalation in slack"
SHORT = "fix it"


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


# --- enable / kill switch ---------------------------------------------------


def test_enhance_enabled_default_on(monkeypatch):
    monkeypatch.delenv("UNIFABLE_PROMPT_ENHANCE", raising=False)
    assert se.enhance_enabled() is True
    monkeypatch.setenv("UNIFABLE_PROMPT_ENHANCE", "1")
    assert se.enhance_enabled() is True
    monkeypatch.setenv("UNIFABLE_PROMPT_ENHANCE", "0")
    assert se.enhance_enabled() is False


# --- path-token grounding ---------------------------------------------------


def test_has_path_token_detects_grounding():
    assert se.has_path_token("edit `lib/foo.mjs` and fix the slice")
    assert se.has_path_token("look at @src/hooks/gate_prompt.py")
    assert se.has_path_token("the bug is in scripts/gate/submit_enhance.py")
    assert se.has_path_token("fix pagination.ts off-by-one")
    assert se.has_path_token("update the Dockerfile")
    assert not se.has_path_token(VAGUE_CODE)
    assert not se.has_path_token("fix the off by one in the pagination slice helper")


# --- under-specified heuristic ----------------------------------------------


def test_is_under_specified_thresholds():
    assert se.is_under_specified(VAGUE_CODE)
    assert not se.is_under_specified(PATHED)  # path token -> grounded
    assert not se.is_under_specified(SHORT)  # < 20 words
    assert not se.is_under_specified("")  # empty


def test_looks_operational_keywords():
    assert se.looks_operational(OPERATIONAL)
    assert se.looks_operational("prep the war room agenda for the churned account")
    assert se.looks_operational("update the jira ticket and the confluence page")
    assert not se.looks_operational(VAGUE_CODE)
    assert not se.looks_operational(PATHED)


# --- fire heuristic (pre-grade) ---------------------------------------------


def test_fire_enhance_vague_code_fires(monkeypatch):
    monkeypatch.delenv("UNIFABLE_PROMPT_ENHANCE", raising=False)
    assert se.fire_enhance(VAGUE_CODE) is True


def test_fire_enhance_skips_pathed_operational_short(monkeypatch):
    monkeypatch.delenv("UNIFABLE_PROMPT_ENHANCE", raising=False)
    assert se.fire_enhance(PATHED) is False
    assert se.fire_enhance(OPERATIONAL) is False
    assert se.fire_enhance(SHORT) is False


def test_fire_enhance_kill_switch(monkeypatch):
    monkeypatch.setenv("UNIFABLE_PROMPT_ENHANCE", "0")
    assert se.fire_enhance(VAGUE_CODE) is False


# --- post-grade use gate ----------------------------------------------------


def test_should_use_enhance_post_grade_gate():
    enhanced = {"enhanced_prompt": "Investigate X in lib/foo.mjs:10-20.", "cited_ranges": ["lib/foo.mjs:10-20"]}
    assert se.should_use_enhance(enhanced, "code", "normal")
    assert se.should_use_enhance(enhanced, "code", "deep")
    assert not se.should_use_enhance(enhanced, "operational", "normal")  # wrong profile
    assert not se.should_use_enhance(enhanced, "code", "quick")  # wrong mode
    assert not se.should_use_enhance(None, "code", "normal")
    assert not se.should_use_enhance({}, "code", "normal")
    assert not se.should_use_enhance({"enhanced_prompt": ""}, "code", "normal")


# --- hard gates -------------------------------------------------------------


def test_contains_repo_cmd_catches_build_test_lint():
    for s in (
        "run pytest tests/test_foo.py",
        "npm test",
        "yarn test",
        "pnpm test",
        "just test",
        "just test-all",
        "make test",
        "cargo test",
        "cargo build",
        "go test ./...",
        "npm run build",
        "pytest -q",
    ):
        assert se.contains_repo_cmd(s), f"missed repo cmd: {s}"


def test_contains_repo_cmd_allows_category_words():
    for s in (
        "run a test that exercises the change",
        "add a typecheck or lint that covers the slice",
        "verify with a build in the relevant category",
        "Investigate lib/foo.mjs:10-20 and lib/bar.ts:5-9",
    ):
        assert not se.contains_repo_cmd(s), f"false positive on: {s}"


def test_gate_enhanced_rejects_repo_cmd():
    out = se.gate_enhanced({"enhanced_prompt": "Fix it then run pytest tests/test_foo.py", "cited_ranges": []})
    assert out is None


def test_gate_enhanced_char_cap():
    long_text = "Investigate the hook. " * 120  # > 1200 chars
    assert len(long_text) > 1200
    out = se.gate_enhanced({"enhanced_prompt": long_text, "cited_ranges": []})
    assert out is not None
    assert len(out["enhanced_prompt"]) <= 1200


def test_gate_enhanced_cited_ranges_clamped_and_typed():
    ranges = [f"lib/file_{i}.py:1-2" for i in range(20)]
    out = se.gate_enhanced({"enhanced_prompt": "do something", "cited_ranges": ranges})
    assert out is not None
    assert len(out["cited_ranges"]) == 8
    # non-list cited_ranges coerces to []
    out2 = se.gate_enhanced({"enhanced_prompt": "do something", "cited_ranges": "not a list"})
    assert out2 is not None and out2["cited_ranges"] == []


def test_gate_enhanced_empty_returns_none():
    assert se.gate_enhanced({"enhanced_prompt": "   ", "cited_ranges": []}) is None
    assert se.gate_enhanced({"enhanced_prompt": "", "cited_ranges": []}) is None


# --- entrypoint resolution --------------------------------------------------


def test_entrypoint_path_resolves_to_explore_script():
    p = se._entrypoint_path()
    assert p is not None, "entrypoint path did not resolve (plugin-root mismatch?)"
    assert p.name == "enhance-prompt.mjs"
    assert p.parent.as_posix().endswith("skills/explore/scripts")
    assert p.is_file(), f"entrypoint missing on disk: {p}"


# --- run_enhancer fail-open matrix ------------------------------------------


def test_run_enhancer_failopen_empty_prompt():
    assert se.run_enhancer("", "/tmp") is None
    assert se.run_enhancer(VAGUE_CODE, "") is None


def test_run_enhancer_failopen_missing_node(monkeypatch):
    monkeypatch.setattr(se.shutil, "which", lambda _name: None)
    assert se.run_enhancer(VAGUE_CODE, "/tmp") is None


def test_run_enhancer_failopen_missing_script(monkeypatch):
    monkeypatch.setattr(se, "_entrypoint_path", lambda: None)
    assert se.run_enhancer(VAGUE_CODE, "/tmp") is None


def test_run_enhancer_failopen_timeout(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd=[], timeout=0)

    monkeypatch.setattr(se.shutil, "which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr(se, "_entrypoint_path", lambda: Path("/fake/enhance-prompt.mjs"))
    monkeypatch.setattr(se.subprocess, "run", _raise)
    assert se.run_enhancer(VAGUE_CODE, "/tmp") is None


def test_run_enhancer_failopen_nonzero_exit(monkeypatch):
    monkeypatch.setattr(se.shutil, "which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr(se, "_entrypoint_path", lambda: Path("/fake/enhance-prompt.mjs"))
    monkeypatch.setattr(se.subprocess, "run", lambda *a, **k: _cp("", returncode=1))
    assert se.run_enhancer(VAGUE_CODE, "/tmp") is None


def test_run_enhancer_failopen_bad_json(monkeypatch):
    monkeypatch.setattr(se.shutil, "which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr(se, "_entrypoint_path", lambda: Path("/fake/enhance-prompt.mjs"))
    monkeypatch.setattr(se.subprocess, "run", lambda *a, **k: _cp("not json at all", returncode=0))
    assert se.run_enhancer(VAGUE_CODE, "/tmp") is None


def test_run_enhancer_failopen_ok_false(monkeypatch):
    monkeypatch.setattr(se.shutil, "which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr(se, "_entrypoint_path", lambda: Path("/fake/enhance-prompt.mjs"))
    monkeypatch.setattr(se.subprocess, "run", lambda *a, **k: _cp('{"ok": false}', returncode=0))
    assert se.run_enhancer(VAGUE_CODE, "/tmp") is None


def test_run_enhancer_failopen_repo_cmd_in_output(monkeypatch):
    monkeypatch.setattr(se.shutil, "which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr(se, "_entrypoint_path", lambda: Path("/fake/enhance-prompt.mjs"))
    payload = '{"ok": true, "enhanced_prompt": "then run pytest tests/test_foo.py", "cited_ranges": []}'
    monkeypatch.setattr(se.subprocess, "run", lambda *a, **k: _cp(payload, returncode=0))
    assert se.run_enhancer(VAGUE_CODE, "/tmp") is None  # hard-gate rejects repo cmd


def test_run_enhancer_returns_gated_on_ok(monkeypatch):
    monkeypatch.setattr(se.shutil, "which", lambda _n: "/usr/bin/node")
    monkeypatch.setattr(se, "_entrypoint_path", lambda: Path("/fake/enhance-prompt.mjs"))
    payload = (
        '{"ok": true, "enhanced_prompt": "Investigate the hook in lib/foo.mjs:10-20 and run a test that exercises it.", '
        '"cited_ranges": ["lib/foo.mjs:10-20", "lib/bar.ts:5-9"]}'
    )
    monkeypatch.setattr(se.subprocess, "run", lambda *a, **k: _cp(payload, returncode=0))
    out = se.run_enhancer(VAGUE_CODE, "/tmp")
    assert out is not None
    assert "lib/foo.mjs" in out["enhanced_prompt"]
    assert out["cited_ranges"] == ["lib/foo.mjs:10-20", "lib/bar.ts:5-9"]


# --- end-to-end policy (enhance_or_none) ------------------------------------


def test_enhance_or_none_post_grade_discards_operational(monkeypatch):
    monkeypatch.delenv("UNIFABLE_PROMPT_ENHANCE", raising=False)
    fake = {"enhanced_prompt": "Investigate lib/foo.mjs:10-20.", "cited_ranges": ["lib/foo.mjs:10-20"]}
    monkeypatch.setattr(se, "run_enhancer", lambda prompt, cwd: fake)
    # grade says operational -> discard
    assert se.enhance_or_none(VAGUE_CODE, "/tmp", "operational", "normal") is None
    # grade says code/normal -> inject
    assert se.enhance_or_none(VAGUE_CODE, "/tmp", "code", "normal") == "Investigate lib/foo.mjs:10-20."
    # grade says code/quick -> discard
    assert se.enhance_or_none(VAGUE_CODE, "/tmp", "code", "quick") is None


def test_enhance_or_none_skips_subprocess_when_fire_false(monkeypatch):
    monkeypatch.delenv("UNIFABLE_PROMPT_ENHANCE", raising=False)
    called = {"n": 0}

    def _spy(prompt, cwd):
        called["n"] += 1
        return None

    monkeypatch.setattr(se, "run_enhancer", _spy)
    # pathed prompt -> fire_enhance False -> run_enhancer never called
    assert se.enhance_or_none(PATHED, "/tmp", "code", "normal") is None
    assert called["n"] == 0
    # operational -> fire_enhance False -> never called
    assert se.enhance_or_none(OPERATIONAL, "/tmp", "code", "normal") is None
    assert called["n"] == 0


def test_build_enhanced_line_strips():
    assert se.build_enhanced_line({"enhanced_prompt": "  lead text  "}) == "lead text"
