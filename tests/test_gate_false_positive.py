#!/usr/bin/env python3
"""Regression: the observation gate must not flag a SUCCESSFUL command as a
failure just because its output contains the words "failed"/"failure"/"error".

This was the Codex false-positive: Codex's shell tool_response is a bare output
string with no exit_code, so the gate fell back to grepping for those words and
fired "observed a tool failure" on `cat`, `grep`, and passing test summaries.
Run: python3 tests/test_gate_false_positive.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

from parse_tool_result import detect_failure  # noqa: E402


def bash(command: str, tool_response):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "tool_response": tool_response}


# (label, payload, expect_failure)
CASES = [
    # --- must stay CLEAN (Codex-style plain-string tool_response, success) ---
    ("cat prints the word 'failure'", bash("cat hooks.json",
        "...unifable gate observed a tool failure. Do not report completion..."), False),
    ("passing pytest '0 failed'", bash("pytest", "=== 12 passed, 0 failed in 1.2s ==="), False),
    ("grep finds 'error:' line", bash("grep -n error: app.log",
        "app.log:42: error: legacy message printed by app"), False),
    ("docs mention 'build failed'", bash("cat README.md",
        "If the build failed, run `make clean`. Common failure modes: ..."), False),
    ("plain success output", bash("echo hi", "hi"), False),
    ("structured exit 0 but text says failed",
        bash("echo x", {"stdout": "the build failed earlier but recovered", "exit_code": 0}), False),
    ("0 tests failed summary", bash("npm test", "Tests: 0 failed, 5 passed"), False),

    # --- file-write tools: content is NOT command output; never infer failure from it ---
    ("Write a doc that mentions 'exit code 2'",
        {"tool_name": "Write", "tool_input": {"file_path": "/p/plan.md", "content": "block = exit code 2"},
         "tool_response": {"type": "create", "filePath": "/p/plan.md", "content": "block = exit code 2"}}, False),
    ("Edit whose patch text says '3 failed'",
        {"tool_name": "Edit", "tool_input": {"file_path": "/p/t.py"},
         "tool_response": {"filePath": "/p/t.py", "content": "assert run() == '3 failed, 1 passed'"}}, False),
    ("Write content with a Traceback example",
        {"tool_name": "Write", "tool_input": {"file_path": "/p/d.md"},
         "tool_response": "Traceback (most recent call last): documenting an error case"}, False),

    # --- must still be FLAGGED (real failures) ---
    ("Bash output 'exit code 2' is STILL a real failure",
        bash("make", "make: *** [build] Error 2\nexit code 2"), True),
    ("structured exit_code 1", bash("pytest", {"stdout": "boom", "exit_code": 1}), True),
    ("python Traceback (plain string)", bash("python x.py",
        "Traceback (most recent call last):\n  File ...\nValueError: bad"), True),
    ("shell command not found", bash("frobnicate", "bash: frobnicate: command not found"), True),
    ("N failed in summary (plain string)", bash("pytest", "2 failed, 3 passed in 0.4s"), True),
    ("rust N previous errors", bash("cargo build",
        "error: could not compile `x` due to 2 previous errors"), True),
    ("rust panic", bash("cargo run", "thread 'main' panicked at src/main.rs:3:5"), True),
    ("structured success:false", bash("deploy", {"success": False, "output": "rolled back"}), True),
]


def main() -> int:
    bad = 0
    for label, payload, expect in CASES:
        got = detect_failure(payload) is not None
        ok = got == expect
        if not ok:
            bad += 1
        print(f"[{'PASS' if ok else 'FAIL'}] expect_failure={expect!s:<5} got={got!s:<5} {label}")
    print(f"\nRESULT: {len(CASES) - bad}/{len(CASES)} passed"
          + ("" if not bad else f" — {bad} REGRESSION(S)"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
