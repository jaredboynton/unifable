#!/usr/bin/env python3
"""Adversarial proof matrix for the unifable spec + evidence gates.

Runs the REAL hooks/pre_tool_use.py via subprocess across an adversarial
scenario matrix and asserts each scenario blocks (exit 2) or allows (exit 0)
exactly as intended. This is the deterministic proof that, when the gate is on,
"evidence before action" is a hard invariant: no edit reaches the repo until a
spec carrying citations (must_read path:line, acceptance_criteria with live
output, prior_art URL for HEAVY) validates.

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


# spec fragments -------------------------------------------------------------
GOOD_ACC = [{"check": "pytest tests/test_x.py -v", "evidence": "5 passed in 0.4s"}]
STD_CITED = {"restated_goal": "Add rate limiting.", "acceptance_criteria": GOOD_ACC,
             "must_read": ["src/mw.py:42", "src/router.py:10-18"]}
HEAVY_FULL = {"restated_goal": "Migrate auth to JWT.", "acceptance_criteria": GOOD_ACC,
              "constraints": ["Keep sessions valid."],
              "rejected_alternatives": ["cookies — stateful.", "hmac — no expiry."],
              "must_read": ["src/auth.py:30"], "prior_art": ["https://datatracker.ietf.org/doc/html/rfc7519"]}

EV = {"UNIFABLE_EVIDENCE_GATE": "1"}
SP = {"UNIFABLE_SPEC_GATE": "1", "UNIFABLE_EVIDENCE_GATE": "0"}


def scenarios(cwd: str):
    """Yield (id, description, expected, env, grade, payload). Specs are written per-scenario."""
    def with_spec(sid, spec):
        save_spec(cwd, sid, spec)
        return sid

    # --- evidence gate: forces citations before an edit ---
    yield ("E1", "evidence-gate STANDARD, no spec", BLOCK, EV, "STANDARD", edit(cwd, "src/a.py", "E1"))
    yield ("E2", "evidence-gate STANDARD, spec missing must_read", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E2", {"restated_goal": "x", "acceptance_criteria": GOOD_ACC})))
    yield ("E3", "evidence-gate STANDARD, cited spec", ALLOW, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E3", STD_CITED)))
    yield ("E4", "evidence-gate must_read malformed (no :line)", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E4", {**STD_CITED, "must_read": ["src/mw.py"]})))
    yield ("E5", "evidence-gate must_read placeholder", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E5", {**STD_CITED, "must_read": ["src/a.py:1 tbd"]})))
    yield ("E6", "evidence-gate acceptance evidence faked", BLOCK, EV, "STANDARD",
           edit(cwd, "src/a.py", with_spec("E6", {**STD_CITED,
                "acceptance_criteria": [{"check": "pytest", "evidence": "not run"}]})))
    yield ("E7", "evidence-gate HEAVY missing prior_art", BLOCK, EV, "HEAVY",
           edit(cwd, "src/a.py", with_spec("E7", {k: v for k, v in HEAVY_FULL.items() if k != "prior_art"})))
    yield ("E8", "evidence-gate HEAVY full (incl prior_art URL)", ALLOW, EV, "HEAVY",
           edit(cwd, "src/a.py", with_spec("E8", HEAVY_FULL)))
    yield ("E9", "evidence-gate HEAVY prior_art not a URL", BLOCK, EV, "HEAVY",
           edit(cwd, "src/a.py", with_spec("E9", {**HEAVY_FULL, "prior_art": ["a blog I read"]})))

    # --- no-brick: research/authoring is never blocked ---
    yield ("N1", "no-brick LIGHT (quick) waives spec", ALLOW, EV, "LIGHT", edit(cwd, "src/a.py", "N1"))
    yield ("N2", "no-brick author the spec file itself", ALLOW, EV, "STANDARD",
           edit(cwd, ".unifable/spec/N2.json", "N2"))
    yield ("N3", "no-brick non-write tool (Bash) under writes-first gate", ALLOW, EV, "STANDARD",
           bash(cwd, "echo hi", "N3"))

    # --- bypass attempts must fail (protected state, traversal) ---
    yield ("B1", "bypass write protected ledger (even with cited spec)", BLOCK, EV, "STANDARD",
           edit(cwd, ".unifable/ledger_x.json", with_spec("B1", STD_CITED)))
    yield ("B2", "bypass path traversal out of spec dir", BLOCK, EV, "STANDARD",
           edit(cwd, ".unifable/spec/../../.unifable/goals.json", with_spec("B2", STD_CITED)))

    # --- spec gate (no evidence requirement) backward-compat ---
    yield ("S1", "spec-gate STANDARD cited-less spec allowed", ALLOW, SP, "STANDARD",
           edit(cwd, "src/a.py", with_spec("S1", {"restated_goal": "x", "acceptance_criteria": GOOD_ACC})))
    yield ("S2", "spec-gate STANDARD no spec", BLOCK, SP, "STANDARD", edit(cwd, "src/a.py", "S2"))

    # --- default-on: no env set => gate is ON; escape hatch disables it ---
    yield ("D1", "default (no gate env): gate ON, uncited edit blocked", BLOCK, {}, "STANDARD",
           edit(cwd, "src/a.py", "D1"))
    yield ("D2", "escape hatch UNIFABLE_EVIDENCE_GATE=0 disables", ALLOW, {"UNIFABLE_EVIDENCE_GATE": "0"},
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
