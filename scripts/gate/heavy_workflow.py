#!/usr/bin/env python3
"""HEAVY-grade frontier-first approach workflow (host-agnostic).

Phases:
  declare  — research only; need restated goal, citations, >=2 frontier tasks, 1 primary
  frontier — explore ALL frontiers; judge marks each rejected/still_viable/accepted
  adopted  — judge compared all explored frontiers and selected the best; primary superseded
  primary  — all frontiers rejected_approach; implement and validate primary fallback
"""

from __future__ import annotations

from typing import Any

APPROACH_KINDS = ("requirement", "frontier", "primary")
HEAVY_PHASES = ("declare", "frontier", "adopted", "primary")

# Statuses that resolve a frontier task for HEAVY phase progression.
# rejected_approach: explored and ruled out; retracted/superseded: judge withdrew
# the frontier requirement; accepted_approach: check passed, viable implementation path.
# All except accepted_approach fall through to the primary fallback.
FRONTIER_RESOLVED = frozenset(
    {
        "rejected_approach",
        "retracted",
        "superseded",
        "accepted_approach",
    }
)


def approach_kind(task: dict[str, Any]) -> str:
    kind = str(task.get("approach_kind") or "requirement").strip().lower()
    return kind if kind in APPROACH_KINDS else "requirement"


def frontier_tasks(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for t in (spec.get("tasks") or []) if isinstance(t, dict) and approach_kind(t) == "frontier"]


def primary_task(spec: dict[str, Any]) -> dict[str, Any] | None:
    primaries = [t for t in (spec.get("tasks") or []) if isinstance(t, dict) and approach_kind(t) == "primary"]
    return primaries[0] if primaries else None


def all_frontiers_rejected(spec: dict[str, Any]) -> bool:
    """True when all frontiers are resolved AND none was accepted_approach."""
    frontiers = frontier_tasks(spec)
    if len(frontiers) < 2:
        return False
    return all(
        str(t.get("status") or "") in FRONTIER_RESOLVED and str(t.get("status") or "") != "accepted_approach" for t in frontiers
    )


def adopted_frontier(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Frontier selected as the adopted implementation (comparison_winner set)."""
    return next(
        (t for t in frontier_tasks(spec) if t.get("comparison_winner") is True),
        None,
    )


def accepted_frontier(spec: dict[str, Any]) -> dict[str, Any] | None:
    """The single frontier selected as best by the comparison round."""
    adopted = adopted_frontier(spec)
    if adopted is not None:
        status = str(adopted.get("status") or "")
        if status in ("accepted_approach", "validated"):
            return adopted
        return None
    return next(
        (
            t
            for t in frontier_tasks(spec)
            if str(t.get("status") or "") == "accepted_approach" and t.get("comparison_winner") is True
        ),
        None,
    )


def frontier_is_resolved(task: dict[str, Any]) -> bool:
    """Whether a frontier task no longer blocks HEAVY completion."""
    status = str(task.get("status") or "")
    if task.get("comparison_winner") is True:
        return True
    if status == "validated":
        return True
    return status in FRONTIER_RESOLVED


def any_frontier_accepted(spec: dict[str, Any]) -> bool:
    """True when at least one frontier is accepted or adoption has completed."""
    if adopted_frontier(spec) is not None:
        return True
    return any(str(t.get("status") or "") == "accepted_approach" for t in frontier_tasks(spec))


def all_frontiers_terminal(spec: dict[str, Any]) -> bool:
    """True when every frontier has a terminal exploration/adoption status."""
    frontiers = frontier_tasks(spec)
    if len(frontiers) < 2:
        return False
    return all(frontier_is_resolved(t) for t in frontiers)


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
    if adopted_frontier(spec) is not None:
        return "adopted"
    if all_frontiers_rejected(spec):
        return "primary"
    return "frontier"


def sync_heavy_phase(spec: dict[str, Any]) -> bool:
    """Recompute and cache heavy_phase on spec. Returns True if mutated."""
    changed = ensure_primary_superseded_on_adoption(spec)
    phase = compute_heavy_phase(spec)
    if spec.get("heavy_phase") != phase:
        spec["heavy_phase"] = phase
        return True
    return changed


def clear_stale_heavy_workflow(spec: dict[str, Any], grade: str) -> bool:
    """Clear a stale heavy_workflow flag on non-HEAVY specs with no approach tasks.

    Genuine HEAVY specs, or specs that still contain frontier/primary approach
    tasks, are left untouched. Returns True when the spec was mutated.
    """
    if str(grade or "").upper() == "HEAVY":
        return False
    if not spec.get("heavy_workflow"):
        return False
    if frontier_tasks(spec) or primary_task(spec) is not None:
        return False
    changed = False
    if spec.get("heavy_workflow"):
        spec["heavy_workflow"] = False
        changed = True
    if spec.get("heavy_phase") is not None:
        spec.pop("heavy_phase", None)
        changed = True
    return changed


def _frontier_exit_code(task: dict[str, Any]) -> int:
    raw = task.get("exit")
    if raw is None:
        return 999
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 999


def _task_id_sort_key(task: dict[str, Any]) -> tuple[int, str]:
    tid = str(task.get("id") or "")
    num = 9999
    if tid.startswith("T"):
        try:
            num = int(tid[1:])
        except ValueError:
            pass
    return num, tid


def ensure_primary_superseded_on_adoption(spec: dict[str, Any]) -> bool:
    """When a frontier is adopted, primary must be superseded (not validated/pending).

    Self-heals specs where the judge set comparison_winner before primary was
    auto-superseded. Idempotent when primary is already superseded.
    """
    winner = adopted_frontier(spec)
    if winner is None:
        return False
    primary = primary_task(spec)
    if primary is None:
        return False
    if str(primary.get("status") or "") == "superseded":
        return False
    winner_id = str(winner.get("id") or "")
    primary["status"] = "superseded"
    primary["judge_reason"] = f"Superseded by adopted frontier {winner_id}."
    return True


def finalize_heavy_adoption(spec: dict[str, Any]) -> list[str]:
    """Deterministically select an adopted frontier and supersede primary.

    Runs when all frontiers are terminal and at least one has accepted_approach.
    No LLM — ranks accepted frontiers by check exit code, then task id.
    Returns human-readable headlines (empty when nothing to do).
    """
    if adopted_frontier(spec) is not None:
        headlines: list[str] = []
        if ensure_primary_superseded_on_adoption(spec):
            winner = adopted_frontier(spec)
            wid = str(winner.get("id") or "") if winner else ""
            headlines.append(f"Primary superseded by adopted frontier {wid}.")
        return headlines
    if not all_frontiers_terminal(spec) or not any_frontier_accepted(spec):
        return []

    accepted = [t for t in frontier_tasks(spec) if str(t.get("status") or "") == "accepted_approach"]
    if not accepted:
        return []

    accepted.sort(
        key=lambda t: (
            0 if _frontier_exit_code(t) == 0 else 1,
            _frontier_exit_code(t),
            *_task_id_sort_key(t),
        )
    )
    winner = accepted[0]
    winner_id = str(winner.get("id") or "")

    headlines: list[str] = []
    for t in frontier_tasks(spec):
        tid = str(t.get("id") or "")
        if tid == winner_id:
            if not t.get("comparison_winner"):
                t["comparison_winner"] = True
                exit_code = t.get("exit")
                headlines.append(f"{tid} selected as adopted frontier (exit {exit_code}).")
        elif str(t.get("status") or "") == "accepted_approach":
            t["status"] = "rejected_approach"
            t["comparison_winner"] = False
            headlines.append(f"{tid} not selected in adoption finalization.")

    if ensure_primary_superseded_on_adoption(spec):
        headlines.append(f"Primary superseded by adopted frontier {winner_id}.")

    before = heavy_snapshot(spec)
    sync_heavy_phase(spec)
    advance_primary_if_ready(spec)
    after = heavy_snapshot(spec)
    transition = heavy_transition_headline(before, after, spec)
    if transition and transition not in headlines:
        headlines.append(transition)

    return headlines


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
        return frontier_is_resolved(task)
    if kind == "primary":
        return status in ("validated", "superseded")
    return status in ("validated", "retracted", "superseded")


def all_tasks_validated_heavy(spec: dict[str, Any]) -> tuple[bool, list[str]]:
    ensure_primary_superseded_on_adoption(spec)
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
    winner = adopted_frontier(spec)
    if winner is not None:
        winner_id = str(winner.get("id") or "")
        incomplete: list[str] = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            kind = approach_kind(t)
            status = str(t.get("status") or "")
            tid = str(t.get("id"))
            if kind == "frontier" and tid == winner_id:
                if status not in ("accepted_approach", "validated"):
                    incomplete.append(tid)
            elif kind == "frontier":
                if not frontier_is_resolved(t):
                    incomplete.append(tid)
            elif kind == "primary":
                # Primary is done when superseded by the adopted frontier OR validated
                # directly (judge approved its delivery). Mirror task_is_resolved so the
                # winner branch matches every other completion path.
                if not task_is_resolved(t):
                    incomplete.append(tid)
            else:
                if not task_is_resolved(t):
                    incomplete.append(tid)
        return (not incomplete), incomplete
    incomplete = [str(t.get("id")) for t in tasks if isinstance(t, dict) and not task_is_resolved(t)]
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
            in_frontier = any(resolved == fs or resolved.startswith(fs.rstrip("/") + "/") for fs in frontier_scopes)
            if not in_frontier:
                return True
    return False


def heavy_workflow_brief(spec: dict[str, Any] | None = None, phase: str | None = None) -> str:
    """Full additionalContext body for first HEAVY trigger and block messages."""
    phase = phase or (compute_heavy_phase(spec) if spec else "declare")
    lines = [
        "HEAVY MODE",
        f"Phase: {phase}",
        "",
        "Glossary: frontier = competing exploratory approach; primary = evidence-backed fallback.",
        "",
        "Declare phase:",
        "1. unifable restate '<goal in your own words>'",
        "2. Read/fetch evidence so repo_context and prior_art sync.",
        "3. unifable set-primary --title '<fallback approach>' --check '<runnable proof>'",
        "4. unifable add-frontier --title '<approach>' --check '<exploration check>' twice, for two distinct approaches",
        "",
        "Frontier phase:",
        "- Explore every frontier.",
        "- Run each frontier check.",
        "- Stop runs frontier adjudication and marks each frontier rejected_approach, still_viable, or accepted_approach.",
        "- Do not edit primary-path scope until all frontiers are terminal.",
        "",
        "After frontier phase:",
        "- If a frontier is adopted, implement that frontier.",
        "- If all frontiers are rejected, implement the primary fallback.",
    ]
    return "\n".join(lines)


def heavy_workflow_phase_hint(spec: dict[str, Any] | None = None, phase: str | None = None) -> str:
    """One-line HEAVY phase reminder when the full brief was already injected."""
    phase = phase or (compute_heavy_phase(spec) if spec else "declare")
    hints = {
        "declare": (
            "HEAVY declare: research only until restated goal, citations, "
            ">=2 frontier tasks, and 1 primary task exist."
        ),
        "frontier": "HEAVY frontier: explore all frontier approaches before primary-path edits.",
        "adopted": "HEAVY adopted: implement the selected frontier; primary is superseded.",
        "primary": "HEAVY primary: all frontiers rejected — implement and validate the primary fallback.",
    }
    return hints.get(phase, f"HEAVY phase: {phase}.")


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


def heavy_snapshot(spec: dict[str, Any]) -> tuple[str, str]:
    """(_heavy_phase, primary_status) snapshot for transition detection.

    Capture before and after a mutation block; pass both to
    heavy_transition_headline to detect a phase flip or primary unblock."""
    primary = primary_task(spec)
    return (
        compute_heavy_phase(spec),
        str(primary.get("status") or "") if primary else "",
    )


def heavy_transition_headline(
    before: tuple[str, str],
    after: tuple[str, str],
    spec: dict[str, Any],
) -> str | None:
    """Headline for a HEAVY phase flip or primary unblock between two snapshots.

    before/after = (heavy_phase, primary_status) from heavy_snapshot(). Returns
    None when nothing workflow-relevant changed."""
    before_phase, before_primary = before
    after_phase, after_primary = after
    if before_phase == after_phase and before_primary == after_primary:
        return None
    primary = primary_task(spec)
    tid = str(primary.get("id") or "primary") if primary else "primary"
    if before_primary == "blocked" and after_primary == "pending":
        return (
            f"HEAVY phase: {after_phase} -- primary task {tid} unblocked "
            "(all frontiers rejected); primary-path edits now allowed."
        )
    if before_phase == "frontier" and after_phase == "adopted":
        winner = accepted_frontier(spec)
        wid = str(winner.get("id") or "") if winner else ""
        return f"HEAVY phase: frontier -> adopted. Frontier {wid} selected as best approach. Primary superseded."
    if before_phase != after_phase:
        return f"HEAVY phase: {before_phase} -> {after_phase}."
    return None
