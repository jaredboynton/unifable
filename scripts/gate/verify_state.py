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

# Host-agnostic safety cap for the COMPLETION breaker (the evidence-spec gate in
# gate_stop). Unlike the observation gate above, the completion breaker is meant
# to block every Stop until every requirement validates -- but with no bound it
# can be trapped forever by a runaway judge that appends requirements faster than
# they validate (see tests/test_judge_runaway.py). This is the circuit-breaker
# "bounded open state": after this many consecutive Stop blocks that make no net
# progress, the breaker releases Stop with a loud escalation instead of trapping
# the session. Kept below the host's own generic Stop-block cap (Claude Code's
# CLAUDE_CODE_STOP_HOOK_BLOCK_CAP defaults to 9) so it fires first AND so Codex /
# other hosts -- which have no such backstop -- are protected too.
# Prior art: martinfowler.com/bliki/CircuitBreaker.html (open state is bounded;
# the breaker resets after a threshold rather than staying open indefinitely).
COMPLETION_MAX_STALLED_BLOCKS = 6


def note_completion_block(ledger: dict[str, Any], incomplete_count: int) -> bool:
    """Track consecutive completion-breaker blocks that make no NET progress.

    Returns True once the breaker has stalled past COMPLETION_MAX_STALLED_BLOCKS,
    meaning Stop must be released (host-agnostic backstop against a judge-driven
    runaway where the task list grows at least as fast as it validates). Progress
    is measured purely from observed state -- a strictly smaller unresolved-task
    count than the previous block resets the streak -- so a legitimately
    converging multi-cycle task is never released early; only a stalled or
    growing one is. Mutates the ledger. Fail-safe: on bad input it counts the
    block as a stall rather than masking a runaway."""
    try:
        incomplete_count = int(incomplete_count)
    except (TypeError, ValueError):
        incomplete_count = 0
    prev = ledger.get("completion_prev_incomplete")
    streak = int(ledger.get("completion_stall_blocks") or 0)
    if isinstance(prev, int) and incomplete_count < prev:
        streak = 0  # fewer unresolved tasks than last block -> genuine progress
    else:
        streak += 1  # stalled or growing -> diverging
    ledger["completion_stall_blocks"] = streak
    ledger["completion_prev_incomplete"] = incomplete_count
    return streak >= COMPLETION_MAX_STALLED_BLOCKS


def reset_completion_stall(ledger: dict[str, Any]) -> None:
    """Clear the stall tracking once the completion breaker opens (all validated)."""
    ledger["completion_stall_blocks"] = 0
    ledger.pop("completion_prev_incomplete", None)


def completion_runaway_warning(incomplete_count: int) -> str:
    """Loud escalation emitted when the completion breaker releases on a runaway."""
    return (
        "unifable completion breaker RELEASED after "
        f"{COMPLETION_MAX_STALLED_BLOCKS} consecutive stops with no net progress "
        f"({incomplete_count} requirement(s) still unvalidated). The judge was "
        "adding requirements at least as fast as they validate (a runaway). "
        "Surfacing for human review instead of trapping the session -- inspect "
        "the spec and reset it (or dispute the spurious requirements) if these "
        "are not real."
    )


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
