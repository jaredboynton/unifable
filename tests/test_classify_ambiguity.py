#!/usr/bin/env python3
"""The uncertainty-hedging rule is now enforced by the judge prompt, not a regex.
This test verifies the judge system prompt instructs the judge to floor hedged
prompts at normal (never quick) and add 'uncertainty' to risk_flags, and that
context_for_mode still surfaces the research nudge.

Run: python3 tests/test_classify_ambiguity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

from classify_task import context_for_mode  # noqa: E402
import grade_override as go  # noqa: E402


def main() -> int:
    bad = 0
    s = go._GRADE_SYSTEM.lower()

    checks = [
        # The judge prompt must instruct hedging floors at normal
        ("hedging language signals research, not quick" in s,
         "judge prompt has hedging->normal rule"),
        ("uncertainty" in s, "judge prompt mentions uncertainty flag"),
        # The context nudge must surface the research instruction
        ("research task" in context_for_mode("normal", ["uncertainty"]).lower()
         and "gather evidence" in context_for_mode("normal", ["uncertainty"]).lower(),
         "uncertainty nudge present in context_for_mode"),
        # parse_grade_verdict must accept uncertainty in risk_flags
        (go.parse_grade_verdict(
            {
                "mode": "normal",
                "risk_flags": ["uncertainty"],
                "reason": "hedged",
                "evidence_profile": "code",
            }
        ) == ("normal", ["uncertainty"], "hedged", "code"),
         "parse_grade_verdict preserves uncertainty flag"),
    ]

    for ok, label in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            bad += 1

    # first_prompt controls whether the cite-evidence footer appears.
    first = context_for_mode("normal", [], first_prompt=True)
    subsequent = context_for_mode("normal", [], first_prompt=False)
    footer_checks = [
        ("Cite evidence" in first, "first_prompt=True includes cite-evidence footer"),
        ("Cite evidence" not in subsequent, "first_prompt=False omits cite-evidence footer"),
    ]
    for ok, label in footer_checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            bad += 1

    total = len(checks) + len(footer_checks)
    print(f"\nRESULT: {total - bad}/{total} passed" + ("" if not bad else f" -- {bad} FAIL"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
