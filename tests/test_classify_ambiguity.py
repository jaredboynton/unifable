#!/usr/bin/env python3
"""Uncertainty hedging is judge-backed, not regex-matched.

Verifies context_for_mode surfaces the research nudge, parse_grade_verdict
preserves the uncertainty flag, and first_prompt controls the cite footer.

Run: python3 tests/test_classify_ambiguity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import grade_override as go  # noqa: E402
from classify_task import context_for_mode  # noqa: E402


def main() -> int:
    bad = 0

    checks = [
        (
            "research task" in context_for_mode("normal", ["uncertainty"]).lower()
            and "gather evidence" in context_for_mode("normal", ["uncertainty"]).lower(),
            "uncertainty nudge present in context_for_mode",
        ),
        (
            go.parse_grade_verdict(
                {
                    "mode": "normal",
                    "risk_flags": ["uncertainty"],
                    "reason": "hedged",
                    "evidence_profile": "code",
                }
            )
            == ("normal", ["uncertainty"], "hedged", "code"),
            "parse_grade_verdict preserves uncertainty flag",
        ),
    ]

    # Uncertainty must not be stated twice in one block: the actionable hedge
    # paragraph covers it, so the bare "Risk flags: uncertainty." enumeration is
    # folded away when uncertainty is the only flag.
    only_uncertain = context_for_mode("normal", ["uncertainty"])
    multi_flag = context_for_mode("normal", ["uncertainty", "churn"])
    fold_checks = [
        ("Risk flags:" not in only_uncertain, "uncertainty-only omits Risk-flags enumeration"),
        ("The prompt hedges" in only_uncertain, "uncertainty hedge paragraph still present"),
        ("Risk flags: churn." in multi_flag, "other flags still enumerated"),
        ("uncertainty" not in multi_flag.split("hedges")[0], "uncertainty excluded from the enumeration line"),
    ]
    checks.extend(fold_checks)

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
