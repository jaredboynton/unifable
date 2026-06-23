#!/usr/bin/env python3
"""Hedging/uncertainty cues must not classify as 'quick' (which waives
verification). They floor a would-be-quick task at 'normal' and attach an
'uncertainty' risk flag that drives a research/grounding nudge — without
over-escalating every hedged sentence to 'deep'. Run: python3 tests/test_classify_ambiguity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

from classify_task import classify_prompt, context_for_mode  # noqa: E402

# (prompt, expected_mode, uncertainty_expected)
CASES = [
    # hedged + otherwise-trivial -> floored to normal, flagged
    ("This probably works but I'm not quite sure why", "normal", True),
    ("Maybe it's a caching thing? perhaps the TTL", "normal", True),
    ("I think this might be a race condition", "normal", True),
    ("not sure if this is thread-safe", "normal", True),
    # explicit 'quick' wins, but uncertainty is still flagged for the nudge
    ("quick question: maybe it's the cache?", "quick", True),
    # genuine deep signal still escalates even with hedging
    ("refactor the auth module, not sure how", "deep", True),
    ("probably need a database migration here", "deep", True),
    # no hedging -> unchanged behavior
    ("implement the login form", "normal", False),
    ("just explain how this works", "quick", False),
    ("fix the failing test", "normal", False),
    # pasted corpus must not force deep when operative ask is prose ops
    ("production pilot " * 50 + "\n❯ cache dispatch send-out-ready", "normal", False),
    # bare 'should' is imperative, NOT uncertainty (precision guard)
    ("you should add a test for this", "normal", False),
]


def main() -> int:
    bad = 0
    for prompt, exp_mode, exp_unc in CASES:
        mode, risks = classify_prompt(prompt)
        unc = "uncertainty" in risks
        ok = mode == exp_mode and unc == exp_unc
        if not ok:
            bad += 1
        print(f"[{'PASS' if ok else 'FAIL'}] mode={mode:<6}(want {exp_mode:<6}) unc={unc!s:<5}(want {exp_unc!s:<5}) :: {prompt}")

    # the uncertainty nudge must surface in the emitted context
    ctx = context_for_mode("normal", ["uncertainty"])
    nudge_ok = "research task" in ctx and "gather evidence" in ctx
    print(f"[{'PASS' if nudge_ok else 'FAIL'}] uncertainty nudge present in context_for_mode")
    if not nudge_ok:
        bad += 1

    total = len(CASES) + 1
    print(f"\nRESULT: {total - bad}/{total} passed" + ("" if not bad else f" — {bad} FAIL"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
