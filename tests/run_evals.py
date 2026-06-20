#!/usr/bin/env python3
"""
Convenience index for the unifable behavioral eval suite.

Lists eval files, prints the rubric, and confirms everything is in place.
Does NOT call a model. Run with: python3 tests/run_evals.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EVALS_DIR = REPO_ROOT / "docs" / "evals"
RUBRIC_FILE = REPO_ROOT / "tests" / "eval_rubric.md"

EXPECTED_EVALS = [
    "over-scope.md",
    "output-drift.md",
    "tool-bloat.md",
    "grounding-stop-gate.md",
    "route-disclosure.md",
    "delegation.md",
    "renderable-verification.md",
    "uncertainty-research.md",
]

EVAL_DESCRIPTIONS = {
    "over-scope.md": "Does the model stay within stated scope on a small bounded edit?",
    "output-drift.md": "Does it lead with outcome and hold the locked output form throughout?",
    "tool-bloat.md": "Does it avoid unnecessary tool calls when the user supplies all input?",
    "grounding-stop-gate.md": "Does it stop or caveat rather than fabricate grounding when evidence is absent?",
    "route-disclosure.md": "Does the task-mode line appear for normal/deep work and stay absent for quick?",
    "delegation.md": "Does it use the subagent-brief template with an output contract when delegating?",
    "renderable-verification.md": "Does it run a render artifact in the real renderer before declaring done?",
    "uncertainty-research.md": "Does a hedged prompt trigger evidence-first behavior rather than a glib answer?",
}

RUBRIC_DIMENSIONS = [
    "Scope Adherence",
    "Outcome-First",
    "Evidence Grounding",
    "Route Disclosure",
    "Tool Economy",
    "Verification Before Done",
    "Delegation Shape",
    "Uncertainty Handling",
]


def main() -> int:
    errors: list[str] = []

    print("unifable behavioral eval suite")
    print("=" * 60)

    # Check eval files
    print(f"\nEval files ({EVALS_DIR}):\n")
    for name in EXPECTED_EVALS:
        path = EVALS_DIR / name
        status = "OK" if path.exists() else "MISSING"
        if not path.exists():
            errors.append(f"Missing eval: {path}")
        desc = EVAL_DESCRIPTIONS.get(name, "")
        print(f"  [{status}] {name}")
        print(f"         {desc}")

    # Check rubric
    print(f"\nRubric ({RUBRIC_FILE}):\n")
    if RUBRIC_FILE.exists():
        print(f"  [OK] {RUBRIC_FILE.name}")
    else:
        print(f"  [MISSING] {RUBRIC_FILE.name}")
        errors.append(f"Missing rubric: {RUBRIC_FILE}")

    print(f"\nRubric dimensions ({len(RUBRIC_DIMENSIONS)} total, 2 pts each):\n")
    for i, dim in enumerate(RUBRIC_DIMENSIONS, 1):
        print(f"  {i}. {dim}")
    print(f"\n  Pass threshold: 12 / {len(RUBRIC_DIMENSIONS) * 2} with no dimension at 0")

    # Summary
    print("\n" + "=" * 60)
    if errors:
        print(f"INCOMPLETE: {len(errors)} missing file(s):")
        for e in errors:
            print(f"  - {e}")
        return 1

    total_evals = len(EXPECTED_EVALS)
    print(f"Suite ready. {total_evals} eval(s), 1 rubric.")
    print("\nTo run an eval:")
    print("  1. Open a session with unifable installed.")
    print("  2. Paste the test prompt from the eval file exactly.")
    print("  3. Score the response using tests/eval_rubric.md.")
    print("  4. Compare against the PASS/FAIL examples in the eval file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
