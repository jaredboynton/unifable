#!/usr/bin/env python3
"""Deterministic spec hygiene before gate validation (no LLM).

Consolidates harness-owned spec fixes that run on PreToolUse, PostToolUse, and
after Stop task adjudication: strip bad auto-sync citations, sync real reads,
finalize HEAVY adoption when evidence allows.
"""

from __future__ import annotations

from typing import Any

try:
    from citations import (
        activity_from_ledger,
        sanitize_harness_citations,
        sync_citations_from_activity,
    )
    from heavy_workflow import finalize_heavy_adoption
except ImportError:  # pragma: no cover
    from scripts.gate.citations import (
        activity_from_ledger,
        sanitize_harness_citations,
        sync_citations_from_activity,
    )
    from scripts.gate.heavy_workflow import finalize_heavy_adoption


def apply_spec_hygiene(
    spec: dict[str, Any],
    activity: dict[str, list[str]],
    cwd: str,
    *,
    added_sink: dict[str, list[str]] | None = None,
) -> tuple[bool, list[str]]:
    """Run deterministic spec hygiene. Returns (mutated, headlines)."""
    if not isinstance(spec, dict):
        return False, []

    changed = False
    headlines: list[str] = []

    removed = sanitize_harness_citations(spec, cwd)
    if removed:
        changed = True
        shown = ", ".join(removed[:3])
        if len(removed) > 3:
            shown += "..."
        headlines.append(
            f"Removed invalid auto-sync citation(s) (path does not exist): {shown}."
        )

    if sync_citations_from_activity(spec, activity, cwd, added_sink=added_sink):
        changed = True

    adopt = finalize_heavy_adoption(spec)
    if adopt:
        changed = True
        headlines.extend(adopt)

    return changed, headlines


def apply_spec_hygiene_from_ledger(
    spec: dict[str, Any],
    ledger: dict[str, Any],
    cwd: str,
    *,
    added_sink: dict[str, list[str]] | None = None,
) -> tuple[bool, list[str]]:
    """Convenience wrapper using ledger activity lists."""
    return apply_spec_hygiene(
        spec, activity_from_ledger(ledger), cwd, added_sink=added_sink,
    )
