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
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "pre_tool_use.py"
sys.path.insert(0, str(REPO / "scripts" / "gate"))
from spec import save_spec  # noqa: E402

BLOCK, ALLOW = "block", "allow"


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
    proc = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    return BLOCK if proc.returncode == 2 else ALLOW


def edit(cwd: str, rel: str, session_id: str = "sess") -> dict:
    return {"tool_name": "Edit",
            "tool_input": {"file_path": os.path.join(cwd, rel), "old_string": "a", "new_string": "b"},
            "session_id": session_id, "cwd": cwd}


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
STD_CITED = {"restated_goal": "Add rate limiting.", "acceptance_criteria": GOOD_ACC,
             "repo_context": MR, "prior_art": PRIOR}
HEAVY_FULL = {
    "restated_goal": "Migrate auth to JWT.",
    "acceptance_criteria": GOOD_ACC,
    "heavy_workflow": True,
    "tasks": [
        {"id": "T1", "title": "Session cookies", "check": "pytest tests/test_sess.py", "status": "pending",
         "approach_kind": "frontier", "added_by": "agent", "exit": None, "output": "",
         "judge_verdict": None, "judge_reason": "", "judge_hint": ""},
        {"id": "T2", "title": "Rotating JWT", "check": "pytest tests/test_jwt.py", "status": "pending",
         "approach_kind": "frontier", "added_by": "agent", "exit": None, "output": "",
         "judge_verdict": None, "judge_reason": "", "judge_hint": ""},
        {"id": "T3", "title": "HMAC bearer", "check": "pytest tests/test_hmac.py", "status": "blocked",
         "approach_kind": "primary", "added_by": "agent", "exit": None, "output": "",
         "judge_verdict": None, "judge_reason": "", "judge_hint": ""},
    ],
    "repo_context": [{"cite": "src/auth.py:30", "why": "auth entrypoint"}],
    "prior_art": [{"cite": "https://datatracker.ietf.org/doc/html/rfc7519", "why": "JWT spec"}],
}

# The evidence gate is unconditional (no env disable). EV is empty: with the gate
# vars scrubbed in run(), the production default (gate ON) is exercised. OFF keeps the
# removed escape env to prove it is now ignored.
EV = {}
OFF = {"UNIFABLE_EVIDENCE_GATE": "0", "UNIFABLE_SPEC_GATE": "0"}


def scenarios(cwd: str):
    """Yield (id, description, expected, env, grade, payload). Specs are written per-scenario."""
    def with_spec(sid, spec):
        save_spec(cwd, sid, spec)
        return sid

    # --- evidence gate: forces citations before an edit ---
    yield ("E1", "evidence-gate STANDARD, no spec", BLOCK, EV, "STANDARD", edit(cwd, "src/a.py", "E1"))
    yield ("E2", "evidence-gate STANDARD, spec missing repo_context", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E2", {"restated_goal": "x", "acceptance_criteria": GOOD_ACC})))
    yield ("E3", "evidence-gate STANDARD, cited spec", ALLOW, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E3", STD_CITED)))
    yield ("E4", "evidence-gate repo_context malformed (no :line)", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E4", {**STD_CITED, "repo_context": [{"cite": "src/mw.py", "why": "hook"}]})))
    yield ("E5", "evidence-gate repo_context placeholder why", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E5", {**STD_CITED, "repo_context": [{"cite": "src/a.py:1", "why": "tbd"}]})))
    yield ("E5b", "evidence-gate repo_context missing why", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E5b", {**STD_CITED, "repo_context": [{"cite": "src/a.py:1", "why": ""}]})))
    yield ("E6b", "evidence-gate STANDARD missing prior_art", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E6b", {k: v for k, v in STD_CITED.items() if k != "prior_art"})))
    yield ("E6", "evidence-gate acceptance evidence faked", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E6", {**STD_CITED,
                "acceptance_criteria": [{"check": "pytest", "evidence": "not run"}]})))
    yield ("E7", "evidence-gate HEAVY missing prior_art", BLOCK, EV, "HEAVY",
           edit(cwd, "src/a.py", with_spec("E7", {k: v for k, v in HEAVY_FULL.items() if k != "prior_art"})))
    yield ("E8", "evidence-gate HEAVY full (incl prior_art {cite,why})", ALLOW, EV, "HEAVY",
           edit(cwd, "src/a.py", with_spec("E8", HEAVY_FULL)))
    yield ("E9", "evidence-gate HEAVY prior_art not a URL", BLOCK, EV, "HEAVY",
           edit(cwd, "src/a.py", with_spec("E9", {**HEAVY_FULL, "prior_art": [{"cite": "a blog I read", "why": "context"}]})))
    yield ("E9b", "evidence-gate prior_art missing why", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E9b", {**STD_CITED, "prior_art": [{"cite": "https://example.com/doc", "why": ""}]})))

    # --- Bash research whitelist (research phase: no valid spec yet) ---
    yield ("BL1", "bash-whitelist rm blocked pre-spec", BLOCK, EV, "STANDARD", bash(cwd, "rm -rf build", "BL1"))
    yield ("BL2", "bash-whitelist git diff blocked pre-spec", BLOCK, EV, "STANDARD",
           bash(cwd, "git diff --stat", "BL2"))
    yield ("BL3", "bash-whitelist echo blocked pre-spec", BLOCK, EV, "STANDARD",
           bash(cwd, "echo hi", "BL3"))
    yield ("BL4", "bash-whitelist pytest blocked pre-spec", BLOCK, EV, "STANDARD",
           bash(cwd, "pytest tests/ -q", "BL4"))
    yield ("BL5", "bash-whitelist cat blocked pre-spec", BLOCK, EV, "STANDARD",
           bash(cwd, "cat README.md", "BL5"))
    yield ("BL6", "bash-whitelist chained ls && cat blocked pre-spec", BLOCK, EV, "STANDARD",
           bash(cwd, "ls && cat README.md", "BL6"))
    yield ("BL7", "bash-whitelist rg allowed pre-spec", ALLOW, EV, "STANDARD",
           bash(cwd, "rg foo src", "BL7"))
    yield ("BL8", "bash-whitelist ls allowed pre-spec", ALLOW, EV, "STANDARD",
           bash(cwd, "ls -la", "BL8"))
    yield ("BL9", "bash-whitelist trace.sh allowed pre-spec", ALLOW, EV, "STANDARD",
           bash(cwd, "bash ./trace.sh --brief auth", "BL9"))
    yield ("BL10", "bash-unlock: valid spec allows mutate (action phase)", ALLOW, EV, "STANDARD",
           bash(cwd, "rm -rf build", with_spec("BL10", STD_CITED)))
    yield ("BL11", "bash-whitelist LIGHT waives", ALLOW, EV, "LIGHT", bash(cwd, "rm -rf build", "BL11"))
    yield ("BL12", "bash-whitelist: removed escape env ignored, non-whitelist still blocked", BLOCK, OFF,
           "STANDARD", bash(cwd, "rm -rf build", "BL12"))
    yield ("DG1", "delegation Task blocked pre-spec", BLOCK, EV, "STANDARD",
           delegate(cwd, "Task", "DG1"))
    yield ("DG2", "delegation Agent blocked pre-spec", BLOCK, EV, "STANDARD",
           delegate(cwd, "Agent", "DG2"))
    yield ("DG3", "delegation LIGHT waives", ALLOW, EV, "LIGHT",
           delegate(cwd, "Task", "DG3"))
    yield ("DG4", "delegation valid spec unlocks action phase", ALLOW, EV, "STANDARD",
           delegate(cwd, "Task", with_spec("DG4", STD_CITED)))

    # --- no-brick: research/authoring is never blocked ---
    yield ("N1", "no-brick LIGHT (quick) waives spec", ALLOW, EV, "LIGHT", edit(cwd, "src/a.py", "N1"))
    yield ("N2", "specs are CLI-only: direct spec edit is blocked", BLOCK, EV, "STANDARD",
           edit(cwd, ".unifable/spec/N2.json", "N2"))
    yield ("N3", "no-brick whitelisted Bash (rg) allowed pre-spec", ALLOW, EV, "STANDARD",
           bash(cwd, "rg --files", "N3"))

    # --- bypass attempts must fail (protected state, traversal) ---
    yield ("B1", "bypass write protected ledger (even with cited spec)", BLOCK, EV, "STANDARD",
           edit(cwd, ".unifable/ledger_x.json", with_spec("B1", STD_CITED)))
    yield ("B2", "bypass path traversal out of spec dir", BLOCK, EV, "STANDARD",
           edit(cwd, ".unifable/spec/../../.unifable/goals.json", with_spec("B2", STD_CITED)))

    # --- removed escape hatch + removed spec-only mode: the old envs are ignored ---
    yield ("S1", "spec-only env does NOT downgrade: cited-less spec still blocked", BLOCK, OFF, "STANDARD",
           edit(cwd, "src/a.py", with_spec("S1", {"restated_goal": "x", "acceptance_criteria": GOOD_ACC})))

    # --- always-on: no env set => gate ON; the removed escape env stays ignored ---
    yield ("D1", "default (no gate env): gate ON, uncited edit blocked", BLOCK, {}, "STANDARD",
           edit(cwd, "src/a.py", "D1"))
    yield ("D2", "removed escape env UNIFABLE_EVIDENCE_GATE=0 ignored, edit still blocked", BLOCK, OFF,
           "STANDARD", edit(cwd, "src/a.py", "D2"))


def main() -> int:
    rows, failures = [], 0
    with tempfile.TemporaryDirectory() as cwd:
        for sid, desc, expected, env, grade, payload in scenarios(cwd):
            actual = run(payload, env, grade)
            ok = actual == expected
            failures += not ok
            rows.append((sid, desc, expected, actual, "PASS" if ok else "FAIL"))

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
