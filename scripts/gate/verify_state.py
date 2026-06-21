#!/usr/bin/env python3
"""Stop-time decision for the unifable observation gate.

The decision is made purely from observed ledger state — never from the
assistant's claim text — so it is language-agnostic. HEAVY-only: it blocks a
HEAVY (deep), non-docs task that changed files but has no OBSERVED successful
verification. This catches "I changed code and tests pass" when no test was
ever run, or ran and failed. STANDARD (normal) no longer hard-blocks (HEAVY-only
— measured noise with no proven benefit). Complementary to finish-the-work.sh
(which catches promise-no-act).

The blocking grade comes from the single policy boundary (evidence_policy): the
caller passes the resolved grade so this gate honours the same UNIFABLE_GRADE
override + precedence the evidence gate uses. When no grade is passed (legacy /
direct callers), it is derived from the ledger's task_mode for back-compat.
"""

from __future__ import annotations

from typing import Any

try:  # bare import on sys.path (hooks + tests); package import otherwise
    from evidence_policy import grade_for_mode
except ImportError:  # pragma: no cover
    from scripts.gate.evidence_policy import grade_for_mode


MAX_STOP_BLOCKS = 2


def has_successful_verification(ledger: dict[str, Any]) -> bool:
    return any(result.get("success") is True for result in ledger.get("verification_results", []))


def docs_only(ledger: dict[str, Any]) -> bool:
    kinds = set(ledger.get("change_kinds", []))
    return bool(ledger.get("changed_files_seen")) and bool(kinds) and kinds <= {"docs"}


def should_block_stop(ledger: dict[str, Any], grade: str | None = None) -> tuple[bool, str]:
    stop_blocks = int(ledger.get("stop_blocks") or 0)
    changed = bool(ledger.get("changed_files_seen"))
    verified = has_successful_verification(ledger)
    # Resolved grade is the enforcement axis. When the caller does not supply one,
    # derive it from the ledger's task_mode classification (back-compat: quick ->
    # LIGHT, normal -> STANDARD, deep -> HEAVY).
    if grade is None:
        grade = grade_for_mode(ledger.get("task_mode") or "quick")
    grade = (grade or "").upper().strip() or "STANDARD"

    if stop_blocks >= MAX_STOP_BLOCKS:
        return False, ""
    if docs_only(ledger):
        return False, ""
    # Block only when a HEAVY (deep) turn actually changed something and ran no
    # observed verification. A HEAVY turn that changed nothing (analysis/planning/
    # reading) has nothing to verify, so it is NOT blocked — the old "add
    # observable proof" nag was a false-positive on ~1/3 of deep read-only firings
    # (measured). LIGHT (quick) and STANDARD (normal) do not hard-block here.
    if grade == "HEAVY" and changed and not verified:
        return True, "unifable gate: run the narrowest verification command for the changed behavior before final response, or record why none applies."
    return False, ""


def warning_after_max_blocks(ledger: dict[str, Any]) -> str:
    if int(ledger.get("stop_blocks") or 0) >= MAX_STOP_BLOCKS and not has_successful_verification(ledger):
        return "unifable gate: verification evidence is still missing — include that gap in the final report."
    return ""
