#!/usr/bin/env python3
"""Regression: the UserPromptSubmit router must never emit stdout that Codex
rejects with "hook returned invalid user prompt submit JSON output".

Codex's exit-0 contract (codex-rs/hooks/src/engine/output_parser.rs):
  - parse stdout as JSON; if it is a JSON OBJECT, use it.
  - else if it "looks like JSON" (trimmed starts with '{' or '[') -> FAIL.
  - else -> treat as plain-text additionalContext (fine).
The router's pack lines start with "[unifable:...]" — a leading '[' that Codex
reads as a malformed JSON array. The fix wraps matches in a JSON object. This
test replicates Codex's decision exactly and asserts no prompt triggers FAIL.
Run: python3 tests/test_router_codex_json.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROUTER = ROOT / "hooks" / "router.sh"


def codex_verdict(stdout: str) -> str:
    """Mirror codex parse_user_prompt_submit + looks_like_json for exit code 0."""
    trimmed = stdout.strip()
    if trimmed:
        try:
            value = json.loads(trimmed)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return "OK_JSON"
    lstripped = stdout.lstrip()
    if lstripped.startswith("{") or lstripped.startswith("["):
        return "FAIL"  # looks like JSON but did not parse as object
    return "OK_PLAINTEXT"  # empty or plain text -> additionalContext


# Prompts chosen to hit every routing branch, multi-pack joins, and tricky chars.
PROMPTS = [
    "debug this failing test, root cause the bug",
    "implement the judge feature and build the pipeline",
    "design the architecture and choose an approach",
    "render an svg chart on the canvas for the website",
    "delegate this to a subagent and orchestrate in parallel",
    "debug and implement and design and render and delegate all at once",
    'implement "quoted" text\nwith a newline and a bug',
    "plain greeting with no routing signal",
    "",  # empty prompt -> early exit, empty stdout
]


def main() -> int:
    bad = 0
    for prompt in PROMPTS:
        payload = json.dumps({"prompt": prompt, "session_id": "t", "cwd": "/tmp"})
        proc = subprocess.run(
            ["bash", str(ROUTER)], input=payload, capture_output=True, text=True
        )
        verdict = codex_verdict(proc.stdout)
        ok = verdict != "FAIL" and proc.returncode == 0
        # when a pack matches, output must be a valid JSON object with additionalContext
        if proc.stdout.strip():
            try:
                obj = json.loads(proc.stdout.strip())
                hso = obj.get("hookSpecificOutput", {})
                ok = ok and hso.get("hookEventName") == "UserPromptSubmit" and bool(
                    hso.get("additionalContext")
                )
            except json.JSONDecodeError:
                ok = False
        if not ok:
            bad += 1
        label = (prompt[:40] + "…") if len(prompt) > 40 else (prompt or "<empty>")
        print(f"[{'PASS' if ok else 'FAIL'}] {verdict:12} :: {label}")

    total = len(PROMPTS)
    print(f"\nRESULT: {total - bad}/{total} passed" + ("" if not bad else f" — {bad} FAIL"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
