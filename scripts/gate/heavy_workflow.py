#!/usr/bin/env python3
"""HEAVY-grade frontier-first approach workflow (host-agnostic).

Phases:
  declare  — research only; need restated goal, citations, >=2 frontier tasks, 1 primary
  frontier — explore frontiers; primary task stays blocked
  primary  — all frontiers rejected_approach; implement and validate primary fallback
"""

from __future__ import annotations

from typing import Any

APPROACH_KINDS = ("requirement", "frontier", "primary")
HEAVY_PHASES = ("declare", "frontier", "primary")

# Statuses that resolve a frontier task (judge-only transition to rejected_approach).
FRONTIER_RESOLVED = frozenset({"rejected_approach"})


def approach_kind(task: dict[str, Any]) -> str:
    kind = str(task.get("approach_kind") or "requirement").strip().lower()
    return kind if kind in APPROACH_KINDS else "requirement"


def frontier_tasks(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        t for t in (spec.get("tasks") or [])
        if isinstance(t, dict) and approach_kind(t) == "frontier"
    ]


def primary_task(spec: dict[str, Any]) -> dict[str, Any] | None:
    primaries = [
        t for t in (spec.get("tasks") or [])
        if isinstance(t, dict) and approach_kind(t) == "primary"
    ]
    return primaries[0] if primaries else None


def all_frontiers_rejected(spec: dict[str, Any]) -> bool:
    frontiers = frontier_tasks(spec)
    if len(frontiers) < 2:
        return False
    return all(str(t.get("status") or "") == "rejected_approach" for t in frontiers)


def heavy_declare_complete(spec: dict[str, Any]) -> bool:
    """True when declare-phase requirements are met (unlocks frontier-phase edits)."""
    if spec.get("goal_seeded"):
        return False
    goal = str(spec.get("restated_goal") or "").strip()
    if not goal:
        return False
    if len(frontier_tasks(spec)) < 2:
        return False
    primary = primary_task(spec)
    if primary is None:
        return False
    if not str(primary.get("title") or "").strip() or not str(primary.get("check") or "").strip():
        return False
    return True


def compute_heavy_phase(spec: dict[str, Any]) -> str:
    if not heavy_declare_complete(spec):
        return "declare"
    if all_frontiers_rejected(spec):
        return "primary"
    return "frontier"


def sync_heavy_phase(spec: dict[str, Any]) -> bool:
    """Recompute and cache heavy_phase on spec. Returns True if mutated."""
    phase = compute_heavy_phase(spec)
    if spec.get("heavy_phase") != phase:
        spec["heavy_phase"] = phase
        return True
    return False


def advance_primary_if_ready(spec: dict[str, Any]) -> bool:
    """Unblock primary task when all frontiers are rejected_approach. Returns True if mutated."""
    changed = sync_heavy_phase(spec)
    if compute_heavy_phase(spec) != "primary":
        return changed
    primary = primary_task(spec)
    if primary is None:
        return changed
    if str(primary.get("status") or "") == "blocked":
        primary["status"] = "pending"
        return True
    return changed


def task_is_resolved(task: dict[str, Any]) -> bool:
    """Whether a task no longer blocks HEAVY completion."""
    status = str(task.get("status") or "")
    kind = approach_kind(task)
    if kind == "frontier":
        return status in FRONTIER_RESOLVED
    if kind == "primary":
        return status == "validated"
    return status in ("validated", "retracted")


def all_tasks_validated_heavy(spec: dict[str, Any]) -> tuple[bool, list[str]]:
    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        if spec.get("requires_tasks"):
            return False, ["<no requirements added yet>"]
        return True, []
    frontiers = frontier_tasks(spec)
    if len(frontiers) < 2:
        return False, ["<need >=2 frontier approach tasks>"]
    if primary_task(spec) is None:
        return False, ["<need primary approach task>"]
    incomplete = [
        str(t.get("id")) for t in tasks
        if isinstance(t, dict) and not task_is_resolved(t)
    ]
    return (not incomplete), incomplete


def _scope_paths(task: dict[str, Any]) -> list[str]:
    raw = task.get("scope_paths") or []
    if not isinstance(raw, list):
        return []
    return [str(p).strip() for p in raw if str(p).strip()]


def frontier_scope_union(spec: dict[str, Any]) -> set[str]:
    paths: set[str] = set()
    for t in frontier_tasks(spec):
        paths.update(_scope_paths(t))
    return paths


def primary_scope_paths(spec: dict[str, Any]) -> list[str]:
    primary = primary_task(spec)
    return _scope_paths(primary) if primary else []


def edit_targets_primary_scope(spec: dict[str, Any], target: str, cwd: str) -> bool:
    """True when an edit path falls in primary scope but outside frontier scopes (frontier phase)."""
    if compute_heavy_phase(spec) != "frontier":
        return False
    primary_paths = primary_scope_paths(spec)
    if not primary_paths:
        return False
    try:
        from pathlib import Path
        resolved = str(Path(target).resolve())
        cwd_res = str(Path(cwd).resolve())
    except (ValueError, OSError):
        return False
    frontier_scopes = frontier_scope_union(spec)
    for pp in primary_paths:
        try:
            p = str(Path(pp).resolve()) if not Path(pp).is_absolute() else str(Path(cwd_res, pp).resolve())
        except (ValueError, OSError):
            p = pp
        if resolved == p or resolved.startswith(p.rstrip("/") + "/"):
            in_frontier = any(
                resolved == fs or resolved.startswith(fs.rstrip("/") + "/")
                for fs in frontier_scopes
            )
            if not in_frontier:
                return True
    return False


def heavy_workflow_brief(spec: dict[str, Any] | None = None, phase: str | None = None) -> str:
    """Full additionalContext body for first HEAVY trigger and block messages."""
    phase = phase or (compute_heavy_phase(spec) if spec else "declare")
    lines = [
        "unifable HEAVY workflow (frontier-first):",
        f"  current phase: {phase}",
        "  declare -> frontier -> primary -> done",
        "",
        "Phase rules:",
        "  declare: research only (reads/fetches). No edits until you have:",
        "    - restated goal (unifable restate '<outcome>')",
        "    - repo_context + prior_art (auto-sync from reads/fetches)",
        "    - >=2 frontier approach tasks (judge may auto-add during research)",
        "    - 1 primary approach task (evidence-backed fallback)",
        "  frontier: explore/implement frontier tasks. Primary stays BLOCKED.",
        "    Only the judge may mark a frontier rejected_approach (on Stop).",
        "  primary: after ALL frontiers are rejected_approach, implement primary.",
        "    Judge validates primary delivery on Stop.",
        "",
        "Manual CLI (append-only; never edit spec JSON):",
        "  unifable restate '<intended outcome>'",
        "  unifable set-primary --title '...' --check '<runnable proof>'",
        "  unifable add-frontier --title '...' --check '<exploration check>'",
        "  unifable add-task --title '...' --check '...'  (non-approach requirements)",
        "  unifable dispute --task <id> --evidence '...'  (impossibility only)",
        "",
        "Judge (background, you are notified via additionalContext):",
        "  - May append frontier tasks while you research (added_by=judge)",
        "  - Adjudicates frontier exploration on Stop -> rejected_approach or still pending",
        "  - Validates primary delivery after frontier phase completes",
        "",
        "Primary-path edits are hard-blocked during frontier phase when scope_paths are set.",
    ]
    return "\n".join(lines)


def format_approach_board(spec: dict[str, Any]) -> str:
    """Compact phase + approach task summary for status notifications."""
    phase = compute_heavy_phase(spec)
    lines = [f"heavy_phase: {phase}"]
    for t in spec.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        kind = approach_kind(t)
        if kind == "requirement":
            continue
        tid = str(t.get("id") or "")
        status = str(t.get("status") or "")
        title = str(t.get("title") or "")[:60]
        by = str(t.get("added_by") or "agent")
        lines.append(f"  [{kind}] {tid} ({status}, {by}): {title}")
    return "\n".join(lines)
