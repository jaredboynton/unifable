#!/usr/bin/env python3
"""Adversarial proof matrix for the unifable spec + evidence gates.

Runs the REAL hooks/pre_tool_use.py via subprocess across an adversarial
scenario matrix and asserts each scenario blocks (exit 2) or allows (exit 0)
exactly as intended. This is the deterministic proof that, when the gate is on,
"evidence before action" is a hard invariant: no edit reaches the repo until a
spec carrying citations (repo_context {cite, why}, acceptance_criteria with live
output, prior_art URL — all at STANDARD+) validates.

Run:  python3 tests/eval_gate_proof.py
Exit: 0 if every scenario matches its expectation, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "pre_tool_use.py"
sys.path.insert(0, str(REPO / "scripts" / "gate"))
from spec import save_spec  # noqa: E402

BLOCK, ALLOW = "block", "allow"
Scenario = tuple[str, str, str, dict, str, Callable[[str], dict]]


def run(payload: dict, env_extra: dict, grade: str) -> str:
    env = dict(os.environ)
    # Scrub inherited gate vars so each scenario fully controls the gate state;
    # an empty env_extra therefore exercises the production default (gate ON).
    env.pop("UNIFABLE_SPEC_GATE", None)
    env.pop("UNIFABLE_EVIDENCE_GATE", None)
    # This harness proves the FORMAT/structure evidence gate; citation truth-checking
    # (does the activity back the citations?) has its own suite, tests/test_citation_verify.py.
    env["UNIFABLE_VERIFY_CITATIONS"] = "0"
    env["UNIFABLE_GRADE"] = grade
    env.update(env_extra)
    proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=env)
    return BLOCK if proc.returncode == 2 else ALLOW


def edit(cwd: str, rel: str, session_id: str = "sess") -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": os.path.join(cwd, rel), "old_string": "a", "new_string": "b"},
        "session_id": session_id,
        "cwd": cwd,
    }


def bash(cwd: str, cmd: str, session_id: str = "sess") -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": session_id, "cwd": cwd}


def delegate(cwd: str, tool_name: str, session_id: str = "sess") -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"description": "inspect auth flow", "prompt": "Read only and report findings."},
        "session_id": session_id,
        "cwd": cwd,
    }


# spec fragments -------------------------------------------------------------
GOOD_ACC = [{"check": "pytest tests/test_x.py -v", "evidence": "5 passed in 0.4s"}]
MR = [{"cite": "src/mw.py:42", "why": "rate-limit hook"}, {"cite": "src/router.py:10-18", "why": "routes wrapped"}]
PRIOR = [{"cite": "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429", "why": "429 retry semantics"}]
STD_CITED = {"restated_goal": "Add rate limiting.", "acceptance_criteria": GOOD_ACC, "repo_context": MR, "prior_art": PRIOR}
HEAVY_FULL = {
    "restated_goal": "Migrate auth to JWT.",
    "acceptance_criteria": GOOD_ACC,
    "heavy_workflow": True,
    "tasks": [
        {
            "id": "T1",
            "title": "Session cookies",
            "check": "pytest tests/test_sess.py",
            "status": "pending",
            "approach_kind": "frontier",
            "added_by": "agent",
            "exit": None,
            "output": "",
            "judge_verdict": None,
            "judge_reason": "",
            "judge_hint": "",
        },
        {
            "id": "T2",
            "title": "Rotating JWT",
            "check": "pytest tests/test_jwt.py",
            "status": "pending",
            "approach_kind": "frontier",
            "added_by": "agent",
            "exit": None,
            "output": "",
            "judge_verdict": None,
            "judge_reason": "",
            "judge_hint": "",
        },
        {
            "id": "T3",
            "title": "HMAC bearer",
            "check": "pytest tests/test_hmac.py",
            "status": "blocked",
            "approach_kind": "primary",
            "added_by": "agent",
            "exit": None,
            "output": "",
            "judge_verdict": None,
            "judge_reason": "",
            "judge_hint": "",
        },
    ],
    "repo_context": [{"cite": "src/auth.py:30", "why": "auth entrypoint"}],
    "prior_art": [{"cite": "https://datatracker.ietf.org/doc/html/rfc7519", "why": "JWT spec"}],
}

# The evidence gate is unconditional (no env disable). EV is empty: with the gate
# vars scrubbed in run(), the production default (gate ON) is exercised. OFF keeps the
# removed escape env to prove it is now ignored.
EV = {}
OFF = {"UNIFABLE_EVIDENCE_GATE": "0", "UNIFABLE_SPEC_GATE": "0"}


def _seed(cwd: str, sid: str, spec: dict) -> str:
    save_spec(cwd, sid, spec)
    return sid


def scenario_specs() -> list[Scenario]:
    """Return (id, description, expected, env, grade, build_payload). Each build uses its own cwd."""
    return [
        ("E1", "evidence-gate STANDARD, no spec", BLOCK, EV, "STANDARD", lambda cwd: edit(cwd, "src/a.py", "E1")),
        (
            "E2",
            "evidence-gate STANDARD, spec missing repo_context",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: edit(cwd, "src/a.py", _seed(cwd, "E2", {"restated_goal": "x", "acceptance_criteria": GOOD_ACC})),
        ),
        (
            "E3",
            "evidence-gate STANDARD, cited spec",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: edit(cwd, "src/a.py", _seed(cwd, "E3", STD_CITED)),
        ),
        (
            "E4",
            "evidence-gate STANDARD, repo_context malformed (no :line)",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: edit(
                cwd, "src/a.py", _seed(cwd, "E4", {**STD_CITED, "repo_context": [{"cite": "src/mw.py", "why": "hook"}]})
            ),
        ),
        (
            "E5",
            "evidence-gate repo_context placeholder why",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: edit(
                cwd, "src/a.py", _seed(cwd, "E5", {**STD_CITED, "repo_context": [{"cite": "src/a.py:1", "why": "tbd"}]})
            ),
        ),
        (
            "E5b",
            "evidence-gate repo_context missing why",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: edit(
                cwd, "src/a.py", _seed(cwd, "E5b", {**STD_CITED, "repo_context": [{"cite": "src/a.py:1", "why": ""}]})
            ),
        ),
        (
            "E6b",
            "evidence-gate STANDARD missing prior_art",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: edit(cwd, "src/a.py", _seed(cwd, "E6b", {k: v for k, v in STD_CITED.items() if k != "prior_art"})),
        ),
        (
            "E6",
            "evidence-gate acceptance evidence faked",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: edit(
                cwd,
                "src/a.py",
                _seed(cwd, "E6", {**STD_CITED, "acceptance_criteria": [{"check": "pytest", "evidence": "not run"}]}),
            ),
        ),
        (
            "E7",
            "evidence-gate HEAVY missing prior_art",
            BLOCK,
            EV,
            "HEAVY",
            lambda cwd: edit(cwd, "src/a.py", _seed(cwd, "E7", {k: v for k, v in HEAVY_FULL.items() if k != "prior_art"})),
        ),
        (
            "E8",
            "evidence-gate HEAVY full (incl prior_art {cite,why})",
            ALLOW,
            EV,
            "HEAVY",
            lambda cwd: edit(cwd, "src/a.py", _seed(cwd, "E8", HEAVY_FULL)),
        ),
        (
            "E9",
            "evidence-gate HEAVY prior_art not a URL",
            BLOCK,
            EV,
            "HEAVY",
            lambda cwd: edit(
                cwd, "src/a.py", _seed(cwd, "E9", {**HEAVY_FULL, "prior_art": [{"cite": "a blog I read", "why": "context"}]})
            ),
        ),
        (
            "E9b",
            "evidence-gate prior_art missing why",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: edit(
                cwd, "src/a.py", _seed(cwd, "E9b", {**STD_CITED, "prior_art": [{"cite": "https://example.com/doc", "why": ""}]})
            ),
        ),
        ("BL1", "bash-whitelist rm blocked pre-spec", BLOCK, EV, "STANDARD", lambda cwd: bash(cwd, "rm -rf build", "BL1")),
        (
            "BL2",
            "bash-whitelist git diff allowed pre-spec",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: bash(cwd, "git diff --stat", "BL2"),
        ),
        (
            "BL2b",
            "bash-whitelist git commit allowed pre-spec",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: bash(cwd, "git commit -m x", "BL2b"),
        ),
        ("BL3", "bash-whitelist echo allowed pre-spec", ALLOW, EV, "STANDARD", lambda cwd: bash(cwd, "echo hi", "BL3")),
        (
            "BL4",
            "bash-whitelist pytest -q allowed pre-spec",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: bash(cwd, "pytest tests/ -q", "BL4"),
        ),
        ("BL5", "bash-whitelist cat allowed pre-spec", ALLOW, EV, "STANDARD", lambda cwd: bash(cwd, "cat README.md", "BL5")),
        (
            "BL6",
            "bash-whitelist chained ls && rm blocked pre-spec",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: bash(cwd, "ls && rm -rf build", "BL6"),
        ),
        ("BL7", "bash-whitelist rg allowed pre-spec", ALLOW, EV, "STANDARD", lambda cwd: bash(cwd, "rg foo src", "BL7")),
        ("BL8", "bash-whitelist ls allowed pre-spec", ALLOW, EV, "STANDARD", lambda cwd: bash(cwd, "ls -la", "BL8")),
        (
            "BL13",
            "bash-whitelist cd && rg allowed pre-spec",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: bash(cwd, "cd subdir && rg foo src", "BL13"),
        ),
        (
            "BL9",
            "bash-whitelist unitrace.sh allowed pre-spec",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: bash(cwd, "bash ./unitrace.sh --brief auth", "BL9"),
        ),
        (
            "BL9c",
            "bash-whitelist unisearch.sh allowed pre-spec",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: bash(cwd, 'bash ./unisearch.sh "task goal"', "BL9c"),
        ),
        (
            "BL9b",
            "bash-whitelist unifusion.sh allowed pre-spec",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: bash(cwd, "bash ./unifusion.sh /tmp/q.txt", "BL9b"),
        ),
        (
            "BL10",
            "bash-unlock: valid spec allows mutate (action phase)",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: bash(cwd, "rm -rf build", _seed(cwd, "BL10", STD_CITED)),
        ),
        ("BL11", "bash-whitelist LIGHT waives", ALLOW, EV, "LIGHT", lambda cwd: bash(cwd, "rm -rf build", "BL11")),
        (
            "BL12",
            "bash-whitelist: removed escape env ignored, non-whitelist still blocked",
            BLOCK,
            OFF,
            "STANDARD",
            lambda cwd: bash(cwd, "rm -rf build", "BL12"),
        ),
        ("DG1", "delegation Task blocked pre-spec", BLOCK, EV, "STANDARD", lambda cwd: delegate(cwd, "Task", "DG1")),
        ("DG2", "delegation Agent blocked pre-spec", BLOCK, EV, "STANDARD", lambda cwd: delegate(cwd, "Agent", "DG2")),
        ("DG3", "delegation LIGHT waives", ALLOW, EV, "LIGHT", lambda cwd: delegate(cwd, "Task", "DG3")),
        (
            "DG4",
            "delegation valid spec unlocks action phase",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: delegate(cwd, "Task", _seed(cwd, "DG4", STD_CITED)),
        ),
        ("N1", "no-brick LIGHT (quick) waives spec", ALLOW, EV, "LIGHT", lambda cwd: edit(cwd, "src/a.py", "N1")),
        (
            "N2",
            "specs are CLI-only: direct spec edit is blocked",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: edit(cwd, ".unifable/spec/N2.json", "N2"),
        ),
        (
            "N3",
            "no-brick whitelisted Bash (rg) allowed pre-spec",
            ALLOW,
            EV,
            "STANDARD",
            lambda cwd: bash(cwd, "rg --files", "N3"),
        ),
        (
            "B1",
            "bypass write protected ledger (even with cited spec)",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: edit(cwd, ".unifable/ledger_x.json", _seed(cwd, "B1", STD_CITED)),
        ),
        (
            "B2",
            "bypass path traversal out of spec dir",
            BLOCK,
            EV,
            "STANDARD",
            lambda cwd: edit(cwd, ".unifable/spec/../../.unifable/goals.json", _seed(cwd, "B2", STD_CITED)),
        ),
        (
            "S1",
            "spec-only env does NOT downgrade: cited-less spec still blocked",
            BLOCK,
            OFF,
            "STANDARD",
            lambda cwd: edit(cwd, "src/a.py", _seed(cwd, "S1", {"restated_goal": "x", "acceptance_criteria": GOOD_ACC})),
        ),
        (
            "D1",
            "default (no gate env): gate ON, uncited edit blocked",
            BLOCK,
            {},
            "STANDARD",
            lambda cwd: edit(cwd, "src/a.py", "D1"),
        ),
        (
            "D2",
            "removed escape env UNIFABLE_EVIDENCE_GATE=0 ignored, edit still blocked",
            BLOCK,
            OFF,
            "STANDARD",
            lambda cwd: edit(cwd, "src/a.py", "D2"),
        ),
    ]


def _run_scenario(spec: Scenario) -> tuple[str, str, str, str, str]:
    sid, desc, expected, env, grade, build = spec
    with tempfile.TemporaryDirectory() as cwd:
        payload = build(cwd)
        actual = run(payload, env, grade)
    ok = actual == expected
    return sid, desc, expected, actual, "PASS" if ok else "FAIL"


def main() -> int:
    specs = scenario_specs()
    order = {spec[0]: idx for idx, spec in enumerate(specs)}
    rows: list[tuple[str, str, str, str, str]] = []
    failures = 0
    workers = min(len(specs), os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_scenario, spec) for spec in specs]
        for fut in as_completed(futures):
            row = fut.result()
            rows.append(row)
            if row[4] != "PASS":
                failures += 1
    rows.sort(key=lambda row: order[row[0]])

    w = max(len(r[1]) for r in rows)
    print(f"{'ID':<4} {'SCENARIO':<{w}} {'EXPECT':<6} {'ACTUAL':<6} RESULT")
    print("-" * (4 + w + 22))
    for sid, desc, exp, act, res in rows:
        print(f"{sid:<4} {desc:<{w}} {exp:<6} {act:<6} {res}")
    total = len(rows)
    print(f"\n{total - failures}/{total} scenarios pass" + ("" if not failures else f"  ({failures} FAILED)"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
