#!/usr/bin/env python3
"""CLI-managed task model: CRUD, status, dedup, supersession (unifable).

Pure task-board operations with no judge calls (the judge/Stop layers build on
this). Host-agnostic; re-exported by the spec.py facade.
"""

from __future__ import annotations

import re
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from heavy_workflow import (
        advance_primary_if_ready,
        all_tasks_validated_heavy,
        primary_task,
        sync_heavy_phase,
    )
    from model_notify import notify_spec_update
except ImportError:  # pragma: no cover
    from scripts.gate.heavy_workflow import (
        advance_primary_if_ready,
        all_tasks_validated_heavy,
        primary_task,
        sync_heavy_phase,
    )
    from scripts.gate.model_notify import notify_spec_update


def find_task(spec: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for t in spec.get("tasks") or []:
        if isinstance(t, dict) and str(t.get("id")) == str(task_id):
            return t
    return None


RESOLVED_STATUSES = ("validated", "retracted", "superseded")


def _is_heavy_spec(spec: dict[str, Any]) -> bool:
    if spec.get("heavy_workflow"):
        return True
    for t in spec.get("tasks") or []:
        if isinstance(t, dict) and str(t.get("approach_kind") or "") in ("frontier", "primary"):
            return True
    return False


def all_tasks_validated(spec: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return (ok, incomplete_ids). ok is True when every task is resolved
    (validated or judge-retracted, or superseded by a replacement requirement).
    legacy acceptance-criteria specs are unaffected -- UNLESS it carries
    `requires_tasks` (set by the auto-creation hook): such a spec must gain >=1
    requirement before it can complete, so an empty one blocks. The agent adds
    requirements; only the judge resolves obsolete or superseded work.

    HEAVY specs with approach tasks use frontier-first completion rules."""
    if _is_heavy_spec(spec):
        advance_primary_if_ready(spec)
        return all_tasks_validated_heavy(spec)
    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        if spec.get("requires_tasks"):
            return False, ["<no requirements added yet>"]
        return True, []
    incomplete = [str(t.get("id")) for t in tasks if not (isinstance(t, dict) and t.get("status") in RESOLVED_STATUSES)]
    return (not incomplete), incomplete


JUDGE_MAX_UNRESOLVED_ADDED = 5


def _task_is_pending(task: dict[str, Any]) -> bool:
    """True when the task is still open and eligible for Stop validation."""
    status = str(task.get("status") or "")
    if status in RESOLVED_STATUSES:
        return False
    if status == "blocked":
        return False
    if str(task.get("approach_kind") or "") == "frontier":
        if status in ("rejected_approach", "accepted_approach", "validated"):
            return False
        if task.get("comparison_winner"):
            return False
    return True


_TITLE_PARENS_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _normalize_title(title: Any) -> str:
    """Normalize a requirement title for duplicate detection: drop a trailing
    parenthetical clause, lowercase, and collapse whitespace. Catches trivially
    reworded re-derivations of the same requirement (case/spacing/parenthetical)
    that the byte-identical (title, check) pair check misses."""
    base = _TITLE_PARENS_RE.sub("", str(title or "").strip())
    return " ".join(base.lower().split())


_PURPOSE_DEDUP_MIN_LEN = 24


_PURPOSE_DEDUP_MIN_CONTAINMENT = 0.65


def _titles_purpose_duplicate(norm_a: str, norm_b: str) -> bool:
    if not norm_a or not norm_b:
        return False
    if norm_a == norm_b:
        return True
    short, long = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    if len(short) < _PURPOSE_DEDUP_MIN_LEN:
        return False
    if not long.startswith(short):
        return False
    return (len(short) / len(long)) >= _PURPOSE_DEDUP_MIN_CONTAINMENT


_SEMVER_LITERAL_RE = re.compile(r"\b\d+\.\d+\.\d+(?:[-+][\w.-]+)?\b")


_VERSION_PIN_CONTEXT_RE = re.compile(
    r"\b("
    r"version|semver|plugin(?:\.json)?|marketplace(?:\.json)?|installed|active\s+plugin|"
    r"release\s+number|pinned|pinning"
    r")\b",
    re.I,
)


def is_brittle_version_pinned_requirement(title: str, check: str) -> bool:
    """True when a requirement hardcodes a semver that will break on every bump."""
    combined = f"{title}\n{check}"
    if not _SEMVER_LITERAL_RE.search(combined):
        return False
    if not _VERSION_PIN_CONTEXT_RE.search(combined):
        return False
    return True


def _norm_title_conflicts(norm: str, existing: set[str]) -> bool:
    return any(_titles_purpose_duplicate(norm, ex) for ex in existing if ex)


def _filter_judge_new_requirements(
    new_reqs: list[dict[str, Any]],
    existing_pairs: set[tuple[str, str]],
    existing_norm_titles: set[str],
) -> list[dict[str, Any]]:
    """Drop purpose-duplicates against the board and within one judge response.

    Prefer the longest (most specific) title when several entries cover the same
    obligation -- e.g. 'verify version 1.9.32' vs 'verify version 1.9.32 in probe'.
    """
    pairs = set(existing_pairs)
    norms = set(existing_norm_titles)
    out: list[dict[str, Any]] = []
    candidates = sorted(
        new_reqs,
        key=lambda r: len(_normalize_title(r.get("title"))),
        reverse=True,
    )
    for req in candidates:
        title = str(req.get("title") or "").strip()
        check = str(req.get("check") or "").strip()
        if not title or not check:
            continue
        pair = (title, check)
        if pair in pairs:
            continue
        norm = _normalize_title(title)
        if not norm or norm in norms or _norm_title_conflicts(norm, norms):
            continue
        if is_brittle_version_pinned_requirement(title, check):
            continue
        out.append(req)
        pairs.add(pair)
        norms.add(norm)
    return out


def detect_requirement_fragmentation(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Detect many failed requirements plus overlapping pending judge additions."""
    tasks = [t for t in (spec.get("tasks") or []) if isinstance(t, dict)]
    failed = [t for t in tasks if str(t.get("status") or "") == "failed"]
    pending_judge = [t for t in tasks if str(t.get("status") or "") in ("pending", "delivered") and t.get("added_by") == "judge"]
    if len(failed) < 3 or not pending_judge:
        return None
    failed_by_norm = {_normalize_title(t.get("title")): str(t.get("id")) for t in failed}
    collisions: list[dict[str, str]] = []
    for p in pending_judge:
        pn = _normalize_title(p.get("title"))
        fid = failed_by_norm.get(pn)
        if fid:
            collisions.append({"pending_id": str(p.get("id")), "failed_id": fid})
    return {
        "failed_count": len(failed),
        "pending_judge_count": len(pending_judge),
        "failed_ids": [str(t.get("id")) for t in failed],
        "pending_judge_ids": [str(p.get("id")) for p in pending_judge],
        "title_collisions": collisions,
    }


def _apply_supersedes_bundle(
    spec: dict[str, Any],
    new_tid: str,
    supersedes_ids: list[str],
    *,
    reason: str = "",
) -> list[str]:
    """Apply supersedes[] from a newly added requirement. Judge-added targets retract;
    agent-authored targets become superseded (non-blocking). Fails open on bad ids."""
    open_statuses = frozenset({"pending", "delivered", "failed"})
    by_id = {str(t.get("id")): t for t in (spec.get("tasks") or []) if isinstance(t, dict)}
    headlines: list[str] = []
    base_reason = reason or f"Superseded by {new_tid}"
    for sid in supersedes_ids:
        sid = str(sid or "").strip()
        if not sid or sid == new_tid:
            continue
        t = by_id.get(sid)
        if t is None or str(t.get("status") or "") not in open_statuses:
            continue
        if t.get("added_by") == "judge":
            t["status"] = "retracted"
            t["judge_reason"] = base_reason
            headline = f"Judge retracted {sid}: {base_reason[:80]}"
        else:
            t["status"] = "superseded"
            t["superseded_by"] = new_tid
            t["judge_reason"] = base_reason
            headline = f"{sid} superseded by {new_tid} (no longer blocking)"
        notify_spec_update(spec, headline, highlight_task=sid)
        headlines.append(headline)
    return headlines


def _current_requirements_payload(spec: dict[str, Any]) -> list[dict[str, str]]:
    """Every requirement on the board (all statuses) for judge duplicate reasoning."""
    out: list[dict[str, str]] = []
    for t in spec.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        entry: dict[str, str] = {
            "id": str(t.get("id")),
            "title": str(t.get("title") or ""),
            "check": str(t.get("check") or ""),
            "status": str(t.get("status") or ""),
            "added_by": str(t.get("added_by") or "agent"),
        }
        kind = str(t.get("approach_kind") or "")
        if kind:
            entry["approach_kind"] = kind
        out.append(entry)
    return out


def append_frontier_task(
    spec: dict[str, Any],
    title: str,
    check: str,
    *,
    added_by: str = "agent",
    scope_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Append a frontier approach task. Mutates spec in place."""
    spec.setdefault("tasks", [])
    spec["heavy_workflow"] = True
    task = _new_task(spec, title, check)
    task["approach_kind"] = "frontier"
    task["added_by"] = added_by
    if scope_paths:
        task["scope_paths"] = scope_paths
    spec["tasks"].append(task)
    sync_heavy_phase(spec)
    return task


def set_primary_task(spec: dict[str, Any], title: str, check: str) -> dict[str, Any]:
    """Set the single primary approach task (blocked until frontiers ruled out)."""
    existing = primary_task(spec)
    if existing is not None:
        raise ValueError("primary approach already set; only one primary task allowed.")
    spec.setdefault("tasks", [])
    spec["heavy_workflow"] = True
    task = _new_task(spec, title, check)
    task["approach_kind"] = "primary"
    task["status"] = "blocked"
    task["added_by"] = "agent"
    spec["tasks"].append(task)
    sync_heavy_phase(spec)
    return task


def _next_task_id(spec: dict[str, Any]) -> str:
    return f"T{len(spec.get('tasks') or []) + 1}"


def _new_task(spec: dict[str, Any], title: str, check: str) -> dict[str, Any]:
    return {
        "id": _next_task_id(spec),
        "title": title.strip(),
        "check": check.strip(),
        "status": "pending",
        "exit": None,
        "output": "",
        "judge_verdict": None,
        "judge_reason": "",
        "judge_hint": "",
        "attempts": 0,
    }
