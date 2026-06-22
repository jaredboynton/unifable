#!/usr/bin/env python3
"""Spec artifact validator and contract helper for the unifable pre-edit gate.

Provides:
  - SPEC_SCHEMA: field definitions (required and optional)
  - FAKE_MARKERS: tuple of placeholder strings that indicate fabricated evidence
  - validate_spec(spec, grade) -> (ok, reasons)
  - check_fake_evidence(text) -> list[str]
  - spec_path(cwd, session_id) -> Path   (global, keyed: <data_root>/specs/<dir_hash>/<session>/spec.json)
  - load_spec(cwd, session_id) -> dict | None
  - save_spec(cwd, session_id, spec) -> Path
  - canonical_project_root(cwd) -> Path   (git root / project markers; subdirs share one spec)
  - spec_template() -> dict
  - CLI: validate / contract (harness) / restate / add-task / set-primary /
    add-frontier / dispute / where (UNIFABLE_DEV=1)

State is one spec.json per (canonical project root, session), so a new session never
inherits a prior one's spec and two repos sharing a session id do not collide. Subdirs
within the same repo resolve to the same canonical root. The CLI always resolves
project root from cwd and session id from the host env (no flags).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from atomicio import write_text_atomic
    from heavy_workflow import (
        advance_primary_if_ready,
        all_frontiers_rejected,
        all_tasks_validated_heavy,
        compute_heavy_phase,
        frontier_tasks,
        heavy_declare_complete,
        heavy_workflow_brief,
        primary_task,
        sync_heavy_phase,
    )
    from ledger import data_root
    from model_notify import format_spec_status, notify_spec_update
except ImportError:  # pragma: no cover
    from scripts.gate.atomicio import write_text_atomic
    from scripts.gate.heavy_workflow import (
        advance_primary_if_ready,
        all_frontiers_rejected,
        all_tasks_validated_heavy,
        compute_heavy_phase,
        frontier_tasks,
        heavy_declare_complete,
        heavy_workflow_brief,
        primary_task,
        sync_heavy_phase,
    )
    from scripts.gate.ledger import data_root
    from scripts.gate.model_notify import format_spec_status, notify_spec_update

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SPEC_SCHEMA: dict[str, dict[str, Any]] = {
    # required
    "restated_goal": {
        "type": str,
        "required": True,
        "description": "The goal restated in the model's own words; must differ from raw ask.",
    },
    "acceptance_criteria": {
        "type": list,
        "required": True,
        "description": "List of {check: <runnable command str>, evidence: <observed output>}.",
    },
    # optional
    "risks": {
        "type": list,
        "required": False,
        "description": "List of risks with blast-radius and mitigation.",
    },
    "non_goals": {
        "type": list,
        "required": False,
        "description": "What is explicitly out of scope.",
    },
    # evidence-gate citation fields (required only when require_evidence=True)
    "repo_context": {
        "type": list,
        "required": False,
        "description": "CODE evidence: 'path:line' citations the model actually read before deciding.",
    },
    "prior_art": {
        "type": list,
        "required": False,
        "description": "RESEARCH evidence: each {cite: 'http(s)://...', why: '<why it backs the approach>'} (docs/repos/papers).",
    },
    # CLI-managed task list. Each task carries a runnable `check`; a task becomes
    # `validated` only when the check runs AND the codex judge confirms the output
    # actually satisfies it. When a spec declares tasks, completion (Stop gate)
    # requires EVERY task validated. Authored and mutated only via spec.py CLI.
    "tasks": {
        "type": list,
        "required": False,
        "description": "List of {id, title, check, status, exit, output, judge_verdict, judge_reason}.",
    },
}

# Grade tier requirements:
#   LIGHT    — restated_goal + >=1 acceptance_criteria (waives spec for trivial changes)
#   STANDARD — full required set (restated_goal + acceptance_criteria)
#   HEAVY    — STANDARD + frontier-first approach workflow (>=2 frontier tasks,
#              1 primary task; see heavy_workflow.py)
GRADES = ("LIGHT", "STANDARD", "HEAVY")

# ---------------------------------------------------------------------------
# Fake-evidence detection
# ---------------------------------------------------------------------------

FAKE_MARKERS: tuple[str, ...] = (
    "not run",
    "assumed",
    "assumption",
    "(assumption)",
    "i assume",
    "presumably",
    "would pass",
    "will pass",
    "should pass",
    "tbd",
    "pending",
    "n/a",
    "todo",
    "will run",
    "placeholder",
    "to be determined",
    "not tested",
    "not verified",
    "not checked",
    "skipped",
    "manually verified",
    "manually tested",
    "trust me",
    "obviously works",
)


def check_fake_evidence(text: str) -> list[str]:
    """Return any FAKE_MARKERS found (case-insensitive) in *text*.

    Used to reject acceptance_criteria evidence fields that contain placeholder
    language rather than live command output.
    """
    lower = (text or "").lower()
    return [marker for marker in FAKE_MARKERS if marker in lower]


# ---------------------------------------------------------------------------
# Citation-format detection (evidence gate)
# ---------------------------------------------------------------------------

# A 'path:line' or 'path:start-end' code citation (e.g. src/app.py:42, a/b.py:10-20).
_PATH_LINE_RE = re.compile(r"^.+:\d+(?:-\d+)?$")
# A source URL.
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def is_path_line(s: str) -> bool:
    """True when *s* looks like a 'path:line' code citation (not a URL)."""
    if not isinstance(s, str):
        return False
    s = s.strip()
    if s.lower().startswith(("http://", "https://")):
        return False
    return bool(_PATH_LINE_RE.match(s))


def is_source_url(s: str) -> bool:
    """True when *s* is an http(s) URL."""
    return isinstance(s, str) and bool(_URL_RE.match(s.strip()))


def repo_context_parts(item: Any) -> tuple[str, str]:
    """Return (cite, why) for a repo_context entry.

    Accepts the required object form {'cite': 'path:line', 'why': '<why relevant>'}.
    A bare 'path:line' string yields (string, '') so the missing-why check fires."""
    if isinstance(item, dict):
        return str(item.get("cite") or item.get("path") or ""), str(item.get("why") or "")
    if isinstance(item, str):
        return item, ""
    return "", ""


def repo_context_of(spec: dict[str, Any]) -> list:
    """Return the spec's repo_context list, falling back to the legacy `must_read`
    key. The field was renamed `must_read` -> `repo_context`; a spec authored under
    the old name (an on-disk spec predating the rename, or a session whose gate
    upgraded mid-flight) must still resolve, or the upgrade strands it: every edit
    is blocked and Stop is blocked, with no in-session way to rewrite the protected
    spec. New specs always write `repo_context`; this is read-side back-compat only.
    Returns the first non-empty list among (repo_context, must_read), else []."""
    for key in ("repo_context", "must_read"):
        val = spec.get(key)
        if isinstance(val, list) and val:
            return val
    return []


def prior_art_parts(item: Any) -> tuple[str, str]:
    """Return (cite, why) for a prior_art entry.

    Accepts the required object form {'cite': 'http(s)://...', 'why': '<why relevant>'}.
    A bare URL string yields (url, '') so the missing-why check fires."""
    if isinstance(item, dict):
        return str(item.get("cite") or item.get("url") or ""), str(item.get("why") or "")
    if isinstance(item, str):
        return item, ""
    return "", ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_spec(
    spec: dict[str, Any], grade: str, require_evidence: bool = False
) -> tuple[bool, list[str]]:
    """Validate *spec* against the requirements for *grade*.

    When *require_evidence* is True (how the hooks always call it), the spec must
    also carry citation evidence at STANDARD+: 'repo_context' (>=1 {cite: 'path:line',
    why: '<why relevant>'}) and 'prior_art' (>=1 {cite: 'http(s)://...', why:
    '<why relevant>'}). This makes the spec the documented evidence that unlocks action.

    Returns (ok, reasons) where reasons is empty when ok is True.
    """
    grade = (grade or "STANDARD").upper()
    if grade not in GRADES:
        return False, [f"Unknown grade '{grade}'; expected one of {', '.join(GRADES)}."]

    reasons: list[str] = []

    if not isinstance(spec, dict):
        return False, ["Spec must be a JSON object."]

    # restated_goal — required for all grades. The auto-creation hook seeds it with
    # the raw prompt and marks `goal_seeded`; that verbatim copy is a placeholder,
    # not a restatement. The agent must rewrite it in its own words (thinking about
    # the intended outcome) via `spec.py restate`, which clears the marker.
    goal = spec.get("restated_goal")
    if not goal or not isinstance(goal, str) or not goal.strip():
        reasons.append("'restated_goal' is required and must be a non-empty string.")
    elif spec.get("goal_seeded"):
        reasons.append(
            "restate the goal in your own words first: restated_goal is still the raw "
            "prompt the hook seeded, not a restatement. Run "
            "`unifable restate '<the intended outcome, in your own words>'`."
        )

    # acceptance_criteria — required for all grades, >=1 item with a non-empty check.
    # A task-spec (CLI-authored, has >=1 task with a check) satisfies this instead:
    # the tasks ARE the acceptance criteria, and their live evidence is produced at
    # validate-task time (judged), not at authoring time.
    tasks = spec.get("tasks")
    has_tasks = isinstance(tasks, list) and any(
        isinstance(t, dict) and str(t.get("check", "")).strip() for t in tasks
    )
    criteria = spec.get("acceptance_criteria")
    if has_tasks:
        pass  # tasks stand in for acceptance_criteria
    elif spec.get("requires_tasks"):
        # Auto-created task-spec with no requirement yet: the agent must add >=1.
        reasons.append(
            "no requirements yet: add at least one with "
            "`unifable add-task --title '<req>' --check '<runnable check>'`."
        )
    elif not isinstance(criteria, list) or not criteria:
        reasons.append("'acceptance_criteria' is required and must contain at least one entry.")
    else:
        for idx, item in enumerate(criteria):
            if not isinstance(item, dict):
                reasons.append(f"acceptance_criteria[{idx}] must be an object with 'check' and 'evidence' keys.")
                continue
            check = item.get("check", "")
            if not isinstance(check, str) or not check.strip():
                reasons.append(f"acceptance_criteria[{idx}].check must be a non-empty runnable command string.")
            evidence = item.get("evidence", "")
            if not isinstance(evidence, str) or not evidence.strip():
                reasons.append(f"acceptance_criteria[{idx}].evidence must be a non-empty string.")
            else:
                fakes = check_fake_evidence(evidence)
                if fakes:
                    reasons.append(
                        f"acceptance_criteria[{idx}].evidence is an unproven assumption/placeholder "
                        f"({fakes}). The gate rejects assumptions -- prove it: paste live output "
                        "(cmd -> output), a code citation (path:line), or a source URL."
                    )

    # HEAVY: frontier-first approach workflow (constraints removed)
    if grade == "HEAVY":
        sync_heavy_phase(spec)
        n_frontier = len(frontier_tasks(spec))
        if n_frontier < 2:
            reasons.append(
                f"HEAVY grade requires >=2 frontier approach tasks "
                f"(have {n_frontier}); use `unifable add-frontier` or wait for judge discovery."
            )
        primary = primary_task(spec)
        if primary is None:
            reasons.append(
                "HEAVY grade requires exactly 1 primary approach task "
                "(evidence-backed fallback); use `unifable set-primary --title ... --check ...`."
            )
        elif not str(primary.get("title") or "").strip() or not str(primary.get("check") or "").strip():
            reasons.append("HEAVY primary approach task must have non-empty title and check.")

    # Evidence gate: citation fields become required at STANDARD+ (LIGHT is exempt
    # because LIGHT waives the spec entirely upstream). Each repo_context citation must
    # carry a 'why relevant' rationale, and prior_art (research/frontier evidence) is
    # required from STANDARD up.
    if require_evidence and grade in ("STANDARD", "HEAVY"):
        repo_context = repo_context_of(spec)  # accepts legacy `must_read` alias
        if not repo_context:
            reasons.append(
                "evidence gate: 'repo_context' is required (list, >=1 "
                "{cite: 'path:line', why: 'why this passage is relevant'})."
            )
        else:
            for idx, item in enumerate(repo_context):
                cite, why = repo_context_parts(item)
                if not is_path_line(cite):
                    reasons.append(
                        f"repo_context[{idx}].cite must be a 'path:line' code citation "
                        f"(e.g. src/app.py:42), got {item!r}."
                    )
                elif check_fake_evidence(cite):
                    reasons.append(
                        f"repo_context[{idx}].cite is an unproven assumption/placeholder ({cite!r}). "
                        "The gate rejects assumptions -- cite a real path:line you read."
                    )
                if not why.strip():
                    reasons.append(
                        f"repo_context[{idx}] needs a non-empty 'why' (why the passage is relevant); "
                        f"use {{'cite': '{cite or 'path:line'}', 'why': '...'}}."
                    )
                elif check_fake_evidence(why):
                    reasons.append(
                        f"repo_context[{idx}].why is an unproven assumption/placeholder ({why!r}). "
                        "The gate rejects assumptions -- prove why the passage is relevant."
                    )

        # prior_art — required from STANDARD up. Each entry must carry a source URL
        # AND a 'why relevant' rationale (mirrors repo_context): a bare URL is rejected.
        prior_art = spec.get("prior_art")
        if not isinstance(prior_art, list) or not prior_art:
            reasons.append(
                "evidence gate: 'prior_art' is required (list, >=1 "
                "{cite: 'http(s)://...', why: 'why this source backs the approach'})."
            )
        else:
            for idx, item in enumerate(prior_art):
                cite, why = prior_art_parts(item)
                if not is_source_url(cite):
                    reasons.append(
                        f"prior_art[{idx}].cite must be a source URL (http(s)://...), got {item!r}."
                    )
                if not why.strip():
                    reasons.append(
                        f"prior_art[{idx}] needs a non-empty 'why' (why this source backs the "
                        f"approach); use {{'cite': '{cite or 'http(s)://...'}', 'why': '...'}}."
                    )
                elif check_fake_evidence(why):
                    reasons.append(
                        f"prior_art[{idx}].why is an unproven assumption/placeholder ({why!r}). "
                        "The gate rejects assumptions -- prove why this source is relevant."
                    )

    return not reasons, reasons


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def resolve_session_id(input_data: dict | None = None, default: str | None = "default") -> str | None:
    """Resolve the per-session key for spec artifacts, consistent across hosts.

    Precedence:
      1. explicit ``session_id`` in the hook payload (Claude Code sends it on
         stdin) -- keeps Claude Code behaviour unchanged,
      2. ``CLAUDE_CODE_SESSION_ID`` (Claude Code env),
      3. ``CODEX_THREAD_ID`` (Codex env),
      4. *default*.

    Hosts that omit ``session_id`` from the hook payload (Codex) and CLI tools
    with no stdin still key the spec per conversation via the env vars both
    runtimes export, instead of colliding on one shared file. Callers that want
    to fail open when nothing resolves pass ``default=None``.
    """
    val, _src = resolve_session_id_with_source(input_data, default)
    return val


def resolve_session_id_with_source(input_data: dict | None = None, default: str | None = "default") -> tuple[str | None, str]:
    """Resolve session id and report the source for diagnostics.

    Returns (value, source) where source is one of:
      'payload', 'env:CLAUDE_CODE_SESSION_ID', 'env:CODEX_THREAD_ID',
      'env:CURSOR_CONVERSATION_ID', 'env:CURSOR_SESSION_ID', 'default', 'none'.
    This enables empirical checks that Bash subprocesses see the same
    session env as the hook that generated the prompt scaffold.

    Real observed names (from `env` inside each host's shell):
      - Claude Code: CLAUDE_CODE_SESSION_ID
      - Codex:       CODEX_THREAD_ID
      - Cursor:      CURSOR_CONVERSATION_ID  (not CURSOR_SESSION_ID)
    """
    if input_data:
        sid = input_data.get("session_id")
        if sid:
            return str(sid), "payload"
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CURSOR_CONVERSATION_ID", "CURSOR_SESSION_ID"):
        val = os.environ.get(var)
        if val:
            return val, f"env:{var}"
    if default is not None:
        return default, "default"
    return None, "none"


_PROJECT_MARKERS = (".git", "pyproject.toml", "go.mod", "Cargo.toml", "package.json")
_CANONICAL_ROOT_CACHE: dict[str, Path] = {}


def _git_toplevel(start: Path) -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if proc.returncode == 0:
            top = proc.stdout.strip()
            if top:
                return Path(top).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def canonical_project_root(cwd: str | Path | None = None) -> Path:
    """Stable project root for spec keying. Subdirs of the same repo share one spec.

    Precedence: ``UNIFABLE_PROJECT_ROOT`` env, ``git rev-parse --show-toplevel``,
    walk up for common project markers, else resolved *cwd*."""
    override = os.environ.get("UNIFABLE_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    start = Path(cwd or os.getcwd()).resolve()
    cache_key = str(start)
    cached = _CANONICAL_ROOT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    git_root = _git_toplevel(start)
    if git_root is not None:
        _CANONICAL_ROOT_CACHE[cache_key] = git_root
        return git_root

    found = start
    current = start
    while True:
        for marker in _PROJECT_MARKERS:
            if (current / marker).exists():
                found = current
                break
        if current.parent == current:
            break
        current = current.parent

    root = found.resolve()
    _CANONICAL_ROOT_CACHE[cache_key] = root
    return root


_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")


def dir_hash(cwd: str | Path) -> str:
    """Stable 16-hex digest of the canonical project root. Keys spec state by
    project so two repos sharing a session id (or the 'default' fallback) never
    collide; subdirs within one repo share the same hash."""
    resolved = str(canonical_project_root(cwd))
    return hashlib.sha256(resolved.encode("utf-8", "replace")).hexdigest()[:16]


def _safe_session(session_id: str | None) -> str:
    """Filesystem-safe session segment. A raw UUID / CODEX_THREAD_ID passes
    through unchanged; anything unsafe is collapsed; empty falls back to 'default'."""
    s = _SAFE_KEY_RE.sub("-", str(session_id or "").strip()).strip("-")
    return s or "default"


def session_dir(cwd: str | Path, session_id: str | None) -> Path:
    """Per-(directory, session) state directory:
    <data_root>/specs/<dir_hash(cwd)>/<session>/  (data_root honors $UNIFABLE_DATA,
    same global root as the gate ledger). Holds spec.json plus the goals plan."""
    return data_root() / "specs" / dir_hash(cwd) / _safe_session(session_id)


def spec_path(cwd: str | Path, session_id: str | None) -> Path:
    """Canonical path for the session's single evidence spec:
    <data_root>/specs/<dir_hash(cwd)>/<session>/spec.json"""
    root = canonical_project_root(cwd)
    return session_dir(root, session_id) / "spec.json"


def format_spec_location(cwd: str | Path, session_id: str | None) -> str:
    """Human-readable spec key for block messages (labels dirhash vs session-id)."""
    root = canonical_project_root(cwd)
    sid = _safe_session(session_id)
    dh = dir_hash(root)
    path = spec_path(root, session_id)
    return (
        f"session-id: {sid}\n"
        f"project: {root}\n"
        f"dirhash: {dh} (path segment only -- not your session-id)\n"
        f"spec: {path}"
    )


def _read_spec_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _spec_file_substantive(path: Path) -> bool:
    data = _read_spec_file(path)
    if not data:
        return False
    tasks = data.get("tasks")
    if isinstance(tasks, list) and tasks:
        return True
    if data.get("repo_context") or data.get("prior_art"):
        return True
    if data.get("restated_goal") and not data.get("goal_seeded", True):
        return True
    return False


def _find_fragmented_specs(session_id: str | None, canonical_root: Path) -> list[Path]:
    """Other dirhash buckets holding a substantive spec for the same session."""
    safe = _safe_session(session_id)
    specs_root = data_root() / "specs"
    canonical = dir_hash(canonical_root)
    if not specs_root.is_dir():
        return []
    found: list[Path] = []
    for entry in specs_root.iterdir():
        if not entry.is_dir() or entry.name == canonical:
            continue
        candidate = entry / safe / "spec.json"
        if candidate.is_file() and _spec_file_substantive(candidate):
            found.append(candidate)
    return found


def _relocate_spec(from_path: Path, canonical_root: Path, session_id: str | None) -> Path | None:
    data = _read_spec_file(from_path)
    if not data:
        return None
    dest = spec_path(canonical_root, session_id)
    write_text_atomic(dest, json.dumps(data, indent=2, sort_keys=False))
    old_dir = from_path.parent
    try:
        from_path.unlink(missing_ok=True)
        if old_dir.exists() and not any(old_dir.iterdir()):
            old_dir.rmdir()
        grand = old_dir.parent
        if grand.name != dir_hash(canonical_root) and grand.exists() and not any(grand.iterdir()):
            grand.rmdir()
    except OSError:
        pass
    print(
        f"unifable: relocated spec from fragmented dirhash to canonical project root ({dest}).",
        file=sys.stderr,
    )
    return dest


def load_spec(cwd: str | Path, session_id: str | None) -> dict[str, Any] | None:
    """Load and parse the session's spec artifact, returning None on any error.

    When missing at the canonical path, searches other dirhash buckets for the same
    session and relocates a lone substantive match."""
    root = canonical_project_root(cwd)
    path = spec_path(root, session_id)
    if path.exists():
        return _read_spec_file(path)
    fragmented = _find_fragmented_specs(session_id, root)
    if len(fragmented) == 1 and _git_toplevel(root) is not None:
        relocated = _relocate_spec(fragmented[0], root, session_id)
        if relocated:
            return _read_spec_file(relocated)
    return None


def save_spec(cwd: str | Path, session_id: str | None, spec: dict[str, Any]) -> Path:
    """Write *spec* to the session's canonical path, creating parents as needed."""
    root = canonical_project_root(cwd)
    path = spec_path(root, session_id)
    return write_text_atomic(path, json.dumps(spec, indent=2, sort_keys=False))


# ---------------------------------------------------------------------------
# Task model (CLI-managed, judge-validated)
# ---------------------------------------------------------------------------

_OUTPUT_LIMIT = 4000  # cap on captured check output stored in the spec


def find_task(spec: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for t in spec.get("tasks") or []:
        if isinstance(t, dict) and str(t.get("id")) == str(task_id):
            return t
    return None


# A task no longer blocks completion once the judge has resolved it: either the
# work was validated, or the judge accepted a dispute and retracted the requirement
# as impossible. Every other status (pending/delivered/failed/disputed) is open.
RESOLVED_STATUSES = ("validated", "retracted")


def _is_heavy_spec(spec: dict[str, Any]) -> bool:
    if spec.get("heavy_workflow"):
        return True
    for t in spec.get("tasks") or []:
        if isinstance(t, dict) and str(t.get("approach_kind") or "") in ("frontier", "primary"):
            return True
    return False


def all_tasks_validated(spec: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return (ok, incomplete_ids). ok is True when every task is resolved
    (validated or judge-retracted). A spec with no tasks returns (True, []) so
    legacy acceptance-criteria specs are unaffected -- UNLESS it carries
    `requires_tasks` (set by the auto-creation hook): such a spec must gain >=1
    requirement before it can complete, so an empty one blocks. The agent adds
    requirements; only the judge removes them (via dispute -> retracted).

    HEAVY specs with approach tasks use frontier-first completion rules."""
    if _is_heavy_spec(spec):
        advance_primary_if_ready(spec)
        return all_tasks_validated_heavy(spec)
    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        if spec.get("requires_tasks"):
            return False, ["<no requirements added yet>"]
        return True, []
    incomplete = [
        str(t.get("id")) for t in tasks
        if not (isinstance(t, dict) and t.get("status") in RESOLVED_STATUSES)
    ]
    return (not incomplete), incomplete


AUTO_VALIDATE_MAX_TASKS = 3
# Per-check subprocess ceiling. On the Stop path auto_validate_spec further bounds
# each check by the remaining wall-clock budget so a slow check can't outlive the
# host Stop-hook timeout (the codex-thread 10s kill).
_CHECK_TIMEOUT = 600


def _apply_dispute(spec: dict[str, Any], task: dict[str, Any]) -> list[str]:
    """Adjudicate a disputed task and apply the verdict. Mutates spec in place."""
    tid = str(task.get("id") or "")
    headlines: list[str] = []
    verdict, reason, hint = judge_dispute(spec, task, str(task.get("dispute_evidence") or ""))
    task["judge_verdict"] = verdict
    task["judge_reason"] = reason
    task["judge_hint"] = hint
    task["status"] = "retracted" if verdict == 1 else "failed"
    if verdict != 1:
        notify_spec_update(
            spec, f"Dispute rejected for {tid}.",
            highlight_task=tid, judge_reason=str(reason or ""), hint=hint,
        )
        headlines.append(f"{tid}: dispute rejected")
    else:
        headline = f"{tid} retracted — judge accepted impossibility."
        if all_tasks_validated(spec)[0]:
            headline += " Completion breaker open."
        notify_spec_update(
            spec, headline,
            highlight_task=tid, judge_reason=str(reason or ""), hint=hint,
        )
        headlines.append(headline)
    return headlines


def _task_is_pending(task: dict[str, Any]) -> bool:
    """True when auto_validate should run check+judge on this task."""
    status = str(task.get("status") or "")
    if status in RESOLVED_STATUSES:
        return False
    if status == "blocked":
        return False
    if str(task.get("approach_kind") or "") == "frontier" and status == "rejected_approach":
        return False
    return True


def _apply_check_result(
    spec: dict[str, Any], task: dict[str, Any], exit_code: int, output: str,
    verdict: int, reason: str, new_reqs: list[dict[str, str]], hint: str,
    *,
    frontier_outcome: str = "",
) -> list[str]:
    """Record a check+judge outcome on the task and notify. Mutates spec in place."""
    tid = str(task.get("id") or "")
    task["exit"] = exit_code
    task["output"] = output
    task["judge_verdict"] = verdict
    task["judge_reason"] = reason
    task["judge_hint"] = hint
    kind = str(task.get("approach_kind") or "requirement")
    if kind == "frontier":
        if frontier_outcome == "rejected_approach":
            task["status"] = "rejected_approach"
        else:
            task["status"] = "failed"
    else:
        task["status"] = "validated" if verdict == 1 else "failed"
    advance_primary_if_ready(spec)
    sync_heavy_phase(spec)
    added: list[str] = []
    for req in new_reqs:
        spec.setdefault("tasks", [])
        nt = _new_task(spec, req["title"], req["check"])
        nt["added_by"] = "judge"
        spec["tasks"].append(nt)
        added.append(nt["id"])
    if kind == "frontier" and task["status"] == "rejected_approach":
        headline = f"{tid} frontier ruled out by judge: {reason[:80]}."
        if all_frontiers_rejected(spec):
            headline += " All frontiers rejected — primary phase unlocked."
    elif verdict == 1:
        headline = f"{tid} check passed (exit {exit_code}); judge accepted the evidence."
        if added:
            headline += f" Judge added {', '.join(added)}."
        if all_tasks_validated(spec)[0]:
            headline += " Completion breaker open."
    else:
        headline = f"{tid} check ran (exit {exit_code}); judge rejected the evidence."
        if added:
            headline += f" Judge added {', '.join(added)}."
    notify_spec_update(
        spec, headline,
        highlight_task=tid, judge_reason=str(reason or ""), hint=hint,
    )
    return [headline]


def _validate_one_task(spec: dict[str, Any], task: dict[str, Any], cwd: str | Path) -> list[str]:
    """Validate ONE task (dispute adjudication or check+judge). Mutates spec in place."""
    if task.get("status") == "disputed":
        return _apply_dispute(spec, task)
    exit_code, output = run_check(task.get("check", ""), cwd=cwd)
    verdict, reason, new_reqs, hint, frontier_outcome = judge_task(spec, task, exit_code, output)
    return _apply_check_result(
        spec, task, exit_code, output, verdict, reason, new_reqs, hint,
        frontier_outcome=frontier_outcome,
    )


def auto_validate_spec(
    spec: dict[str, Any],
    cwd: str | Path,
    *,
    max_tasks: int = AUTO_VALIDATE_MAX_TASKS,
    time_budget: float | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate up to max_tasks unresolved tasks. Mutates spec in place.

    Disputes are adjudicated individually; check-based tasks have their checks run
    (each bounded by the remaining wall-clock budget) and are then judged together
    in ONE batched WebSocket round-trip, so a Stop validating several tasks costs a
    single handshake and stays within the host timeout. When time_budget is set,
    work stops once the deadline passes -- remaining tasks stay unresolved and are
    picked up on the next stop (convergent), instead of the hook being killed."""
    messages: list[str] = []
    deadline = (time.monotonic() + time_budget) if time_budget is not None else None

    pending: list[dict[str, Any]] = []
    advance_primary_if_ready(spec)
    for task in list(spec.get("tasks") or []):
        if not isinstance(task, dict) or not _task_is_pending(task):
            continue
        pending.append(task)
        if len(pending) >= max_tasks:
            break

    # Frontier tasks before primary during HEAVY workflow
    if _is_heavy_spec(spec):
        pending.sort(key=lambda t: (
            0 if str(t.get("approach_kind") or "") == "frontier" else
            1 if str(t.get("approach_kind") or "") == "primary" else 2
        ))

    # Snapshot the partition BEFORE adjudicating: _apply_dispute mutates status to
    # "retracted", which must not then sweep the task into the check+judge path.
    disputes = [t for t in pending if t.get("status") == "disputed"]
    checks = [t for t in pending if t.get("status") != "disputed"]

    for task in disputes:
        if deadline is not None and time.monotonic() >= deadline:
            return spec, messages
        messages.extend(_apply_dispute(spec, task))

    items: list[dict[str, Any]] = []
    for task in checks:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if deadline is not None:
            ct = max(1, int(min(_CHECK_TIMEOUT, deadline - time.monotonic())))
            exit_code, output = run_check(task.get("check", ""), cwd=cwd, timeout=ct)
        else:
            exit_code, output = run_check(task.get("check", ""), cwd=cwd)
        items.append({"task": task, "exit_code": exit_code, "output": output})

    if items:
        verdicts = judge_tasks(spec, items)
        for it, (verdict, reason, new_reqs, hint, frontier_outcome) in zip(items, verdicts):
            messages.extend(
                _apply_check_result(spec, it["task"], it["exit_code"], it["output"],
                                    verdict, reason, new_reqs, hint,
                                    frontier_outcome=frontier_outcome)
            )
    return spec, messages


def run_check(check: str, cwd: str | Path = ".", timeout: int = _CHECK_TIMEOUT) -> tuple[int, str]:
    """Run a task's check command -> (exit_code, combined stdout+stderr, capped)."""
    try:
        proc = subprocess.run(
            check, shell=True, cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out[-_OUTPUT_LIMIT:]
    except subprocess.TimeoutExpired:
        return 124, f"(check timed out after {timeout}s)"
    except Exception as exc:  # noqa: BLE001
        return 127, f"(check failed to run: {exc})"


_NEW_REQ_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "check": {"type": "string"}},
        "required": ["title", "check"],
        "additionalProperties": False,
    },
}
_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "integer", "enum": [0, 1]},
        "reason": {"type": "string"},
        # The judge may DISCOVER further requirements the goal needs while judging
        # this task. It can only ADD them; it never removes existing ones.
        "new_requirements": _NEW_REQ_SCHEMA,
        # Advisory only. A concrete next step if the agent looks stuck or is making
        # poor judgement. NEVER changes the verdict and NEVER lifts a gate.
        "hint": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}
_DISPUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "integer", "enum": [0, 1]},
        "reason": {"type": "string"},
        # Advisory only, same contract as _JUDGE_SCHEMA.hint.
        "hint": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}

# A dedicated, verdict-free schema for the proactive nudge path (judge_hint):
# the model returns ONLY guidance, never a verdict, so it structurally cannot
# resolve a task or open a breaker.
_HINT_SCHEMA = {
    "type": "object",
    "properties": {"hint": {"type": "string"}},
    "required": ["hint"],
    "additionalProperties": False,
}

# Advisory-hint guidance shared by every judge prompt that can emit a hint. The
# hint is forward-looking ("what to do next"), distinct from `reason` ("why this
# verdict"), and is surfaced to the agent on a clearly-advisory channel.
_HINT_GUIDANCE = (
    "Separately from your verdict, if the agent appears stuck, looping, or making "
    "poor judgement (checks that reference nonexistent files, the same failure "
    "repeating, disputing instead of doing easy work, a spec that has fragmented), "
    "set `hint` to ONE concrete, actionable next step to get unstuck. The hint is "
    "ADVISORY ONLY: it never changes your verdict and never lifts any gate. Leave "
    "it empty when you have nothing genuinely useful to add."
)

# Placeholder tokens that disqualify a hint -- a hint must be concrete, not a
# hedge. Mirrors the assumption-rejection the spec gate applies elsewhere.
_HINT_PLACEHOLDERS = ("tbd", "n/a", "none", "no hint", "nothing", "unsure", "unclear")
_HINT_MAX = 280


def _normalize_hint(raw: Any) -> str:
    """Coerce a judge hint into a clean, capped string. Returns '' for anything
    empty or placeholder-like so a non-hint never reaches the agent."""
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    if text.lower() in _HINT_PLACEHOLDERS:
        return ""
    if len(text) > _HINT_MAX:
        text = text[: _HINT_MAX - 3] + "..."
    return text


def _normalize_new_requirements(raw: Any) -> list[dict[str, str]]:
    """Coerce the judge's new_requirements into a clean [{title, check}] list,
    dropping anything without both fields."""
    out: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                check = str(item.get("check") or "").strip()
                if title and check:
                    out.append({"title": title, "check": check})
    return out


_JUDGE_SYSTEM = (
    "You are a strict, adversarial validator for a software task. You are given "
    "the overall goal, one task with its check command, the command's exit code, "
    "and its captured output. Decide whether the output is real evidence that the "
    "task is genuinely complete and correct -- not merely that a command ran. "
    "Return verdict 1 only if convinced; otherwise 0. Be skeptical of empty "
    "output, errors, skipped or zero tests, and output that does not match the task. "
    "If, while judging, you find the goal needs further requirements not yet "
    "covered by a task, list them in new_requirements as {title, check} with a "
    "runnable check; otherwise return an empty list. You may only ADD requirements. "
    + _HINT_GUIDANCE
)

_FRONTIER_JUDGE_SYSTEM = (
    "You are a strict frontier-approach adjudicator. A frontier approach is a "
    "realistic cutting-edge option the agent was required to explore before falling "
    "back to the evidence-backed primary approach. Given the goal, frontier title, "
    "check command, exit code, and output, decide whether the exploration is "
    "sufficient to RULE OUT this frontier (broken boundary found, failed experiment, "
    "or proven infeasible). Return outcome 'rejected_approach' only when the "
    "evidence convincingly disqualifies the frontier — not merely because the check "
    "failed once. Return 'still_viable' when more exploration is warranted. "
    "Set verdict to 0 always (frontier resolution uses outcome, not verdict). "
    + _HINT_GUIDANCE
)

_PRIMARY_JUDGE_SYSTEM = (
    "You are validating delivery of the evidence-backed PRIMARY fallback approach "
    "after all frontier approaches were ruled out. Return verdict 1 only if the "
    "check output proves the primary approach was implemented correctly. "
    + _HINT_GUIDANCE
)

_DISCOVER_SYSTEM = (
    "You identify realistic cutting-edge frontier approaches worth exploring before "
    "committing to the evidence-backed primary fallback. Given the restated goal and "
    "recent research activity (reads, fetches), propose 0-2 frontier approaches. "
    "Each must be plausible, distinct, and testable with a runnable check command. "
    "Include scope_paths (repo file paths the frontier would touch) when inferrable. "
    "Return an empty frontiers list if nothing useful to add."
)

_DISCOVER_SCHEMA = {
    "type": "object",
    "properties": {
        "frontiers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "check": {"type": "string"},
                    "scope_paths": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "reason": {"type": "string"},
                },
                "required": ["title", "check"],
                "additionalProperties": False,
            },
        },
        "reason": {"type": "string"},
    },
    "required": ["frontiers"],
    "additionalProperties": False,
}

_FRONTIER_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "integer", "enum": [0, 1]},
        "outcome": {"type": "string", "enum": ["rejected_approach", "still_viable"]},
        "reason": {"type": "string"},
        "new_requirements": _NEW_REQ_SCHEMA,
        "hint": {"type": "string"},
    },
    "required": ["verdict", "outcome", "reason"],
    "additionalProperties": False,
}


def _judge_user(spec: dict[str, Any], task: dict[str, Any], exit_code: int, output: str) -> str:
    payload: dict[str, Any] = {
        "goal": spec.get("restated_goal", ""),
        "task_title": task.get("title", ""),
        "check": task.get("check", ""),
        "exit_code": exit_code,
        "output": output,
    }
    kind = str(task.get("approach_kind") or "requirement")
    if kind in ("frontier", "primary"):
        payload["approach_kind"] = kind
        primary = primary_task(spec)
        if primary:
            payload["primary_approach"] = primary.get("title", "")
    return json.dumps(payload, ensure_ascii=False)


def _judge_system_for_task(task: dict[str, Any]) -> str:
    kind = str(task.get("approach_kind") or "requirement")
    if kind == "frontier":
        return _FRONTIER_JUDGE_SYSTEM
    if kind == "primary":
        return _PRIMARY_JUDGE_SYSTEM
    return _JUDGE_SYSTEM


def _judge_schema_for_task(task: dict[str, Any]) -> dict[str, Any]:
    kind = str(task.get("approach_kind") or "requirement")
    if kind == "frontier":
        return _FRONTIER_JUDGE_SCHEMA
    return _JUDGE_SCHEMA


def _judge_result(res: Any, task: dict[str, Any] | None = None) -> tuple[int, str, list[dict[str, str]], str, str]:
    verdict = 1 if isinstance(res, dict) and res.get("verdict") == 1 else 0
    reason = str(res.get("reason") or "") if isinstance(res, dict) else ""
    new_reqs = _normalize_new_requirements(res.get("new_requirements")) if isinstance(res, dict) else []
    hint = _normalize_hint(res.get("hint")) if isinstance(res, dict) else ""
    frontier_outcome = ""
    if task and str(task.get("approach_kind") or "") == "frontier" and isinstance(res, dict):
        outcome = str(res.get("outcome") or "").strip()
        if outcome in ("rejected_approach", "still_viable"):
            frontier_outcome = outcome
        verdict = 0
    return verdict, reason, new_reqs, hint, frontier_outcome


def judge_task(
    spec: dict[str, Any], task: dict[str, Any], exit_code: int, output: str
) -> tuple[int, str, list[dict[str, str]], str, str]:
    """Ask the judge whether a single check output validates the task.

    Returns (verdict, reason, new_requirements, hint, frontier_outcome).
    frontier_outcome is 'rejected_approach' or 'still_viable' for frontier tasks."""
    try:
        from codex_judge import JudgeError, ask_structured
    except ImportError as exc:  # pragma: no cover
        return 0, f"judge unavailable: {exc}", [], "", ""
    try:
        res = ask_structured(
            _judge_system_for_task(task), _judge_user(spec, task, exit_code, output),
            _judge_schema_for_task(task), schema_name="task_verdict",
        )
    except JudgeError as exc:
        return 0, f"judge error: {exc}", [], "", ""
    return _judge_result(res, task)


def judge_tasks(
    spec: dict[str, Any], items: list[dict[str, Any]]
) -> list[tuple[int, str, list[dict[str, str]], str, str]]:
    """Judge several (task, exit_code, output) items, batched into ONE WebSocket
    round-trip when there is more than one."""
    if not items:
        return []
    if len(items) == 1:
        it = items[0]
        return [judge_task(spec, it["task"], it["exit_code"], it["output"])]
    try:
        from codex_judge import JudgeError, ask_structured_batch
    except ImportError as exc:  # pragma: no cover
        return [(0, f"judge unavailable: {exc}", [], "", "") for _ in items]
    reqs = [
        {
            "system": _judge_system_for_task(it["task"]),
            "user": _judge_user(spec, it["task"], it["exit_code"], it["output"]),
            "schema": _judge_schema_for_task(it["task"]),
            "schema_name": "task_verdict",
        }
        for it in items
    ]
    results = ask_structured_batch(reqs)
    out: list[tuple[int, str, list[dict[str, str]], str, str]] = []
    for it, res in zip(items, results):
        if isinstance(res, JudgeError):
            out.append((0, f"judge error: {res}", [], "", ""))
        else:
            out.append(_judge_result(res, it["task"]))
    return out


def _normalize_scope_paths(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(p).strip() for p in raw if str(p).strip()]


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


def judge_discover_frontiers(
    spec: dict[str, Any],
    recent_activity: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Ask judge to propose frontier tasks from research activity. Returns added tasks."""
    if len(frontier_tasks(spec)) >= 2:
        return []
    try:
        from codex_judge import JudgeError, ask_structured
    except ImportError:
        return []
    user = json.dumps({
        "goal": spec.get("restated_goal", ""),
        "existing_frontiers": [t.get("title") for t in frontier_tasks(spec)],
        "read_paths": (recent_activity.get("read_paths") or [])[-20:],
        "fetched_urls": (recent_activity.get("fetched_urls") or [])[-10:],
        "repo_context": spec.get("repo_context") or [],
        "prior_art": spec.get("prior_art") or [],
    }, ensure_ascii=False)
    try:
        res = ask_structured(_DISCOVER_SYSTEM, user, _DISCOVER_SCHEMA, schema_name="frontier_discover")
    except JudgeError:
        return []
    added: list[dict[str, Any]] = []
    frontiers = res.get("frontiers") if isinstance(res, dict) else []
    if not isinstance(frontiers, list):
        return []
    for item in frontiers:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        check = str(item.get("check") or "").strip()
        if not title or not check:
            continue
        if len(frontier_tasks(spec)) >= 2:
            break
        task = append_frontier_task(
            spec, title, check,
            added_by="judge",
            scope_paths=_normalize_scope_paths(item.get("scope_paths")),
        )
        reason = str(item.get("reason") or res.get("reason") or "").strip()
        if reason:
            task["discovery_reason"] = reason
        added.append(task)
    if added:
        ids = ", ".join(t["id"] for t in added)
        notify_spec_update(
            spec,
            f"Judge added frontier approach(s): {ids}. Explore these before primary.",
        )
    return added


def judge_dispute(spec: dict[str, Any], task: dict[str, Any], evidence: str) -> tuple[int, str, str]:
    """Adjudicate an agent's claim that a requirement is IMPOSSIBLE.

    The agent has submitted `evidence` that the task cannot be satisfied. Return
    (verdict, reason, hint): verdict 1 accepts the impossibility (the caller
    retracts the requirement), 0 rejects it (the requirement stays open with
    feedback). hint is advisory-only guidance (never changes the verdict). A judge
    failure returns (0, reason, "") so an unreachable judge never auto-retracts a
    requirement -- impossibility must be earned, not granted by default."""
    try:
        from codex_judge import JudgeError, ask_structured
    except ImportError as exc:  # pragma: no cover
        return 0, f"judge unavailable: {exc}", ""
    system = (
        "You are a strict adjudicator. An agent claims a REQUIRED task is impossible "
        "and submits evidence. Accept (verdict 1) ONLY if the evidence genuinely "
        "proves the task cannot be done -- a real, demonstrated blocker, not a "
        "preference, a difficulty, or an excuse. Reject (verdict 0) if the evidence "
        "is weak, the task is merely hard or inconvenient, or the agent is dodging "
        "work; in reason, tell the agent bluntly what real proof would be required. "
        "Do not accept a claim that work is 'complete' here -- this is only about "
        "whether the requirement is genuinely impossible. "
        + _HINT_GUIDANCE
    )
    user = json.dumps({
        "goal": spec.get("restated_goal", ""),
        "task_title": task.get("title", ""),
        "check": task.get("check", ""),
        "impossibility_evidence": evidence,
    }, ensure_ascii=False)
    try:
        res = ask_structured(system, user, _DISPUTE_SCHEMA, schema_name="dispute_verdict")
    except JudgeError as exc:
        return 0, f"judge error: {exc}", ""
    return (
        1 if res.get("verdict") == 1 else 0,
        str(res.get("reason") or ""),
        _normalize_hint(res.get("hint")),
    )


def judge_hint(spec: dict[str, Any], *, signal: str, recent: str = "") -> str:
    """Proactive, verdict-free nudge for an agent that looks stuck or is wandering.

    Unlike judge_task/judge_dispute, this renders NO verdict and resolves NO task
    -- it returns advisory guidance only. Callers (the Stop completion-breaker loop
    and the PostToolUse repeated-failure loop) surface the returned string on a
    clearly-advisory channel; it can never lift a gate. Any judge failure returns
    "" so a hint never blocks and an unreachable judge is simply silent."""
    try:
        from codex_judge import JudgeError, ask_structured
    except ImportError:  # pragma: no cover
        return ""
    system = (
        "You are a calm, senior engineering lead watching an agent that appears to "
        "be stuck or making poor judgement. You are NOT judging completion, you "
        "CANNOT change any verdict, and you CANNOT lift any gate -- you only offer "
        "ONE concrete, actionable next step to get the agent unstuck. Be specific "
        "and grounded in the goal, the task board, and what the agent has been "
        "doing. If you have nothing genuinely useful to say, return an empty hint."
    )
    board = spec.get("tasks") or []
    user = json.dumps({
        "goal": spec.get("restated_goal", ""),
        "why_it_looks_stuck": signal,
        "tasks": [
            {"id": t.get("id"), "title": t.get("title"),
             "status": t.get("status"), "judge_reason": t.get("judge_reason")}
            for t in board if isinstance(t, dict)
        ],
        "recent_activity": recent[:2000],
    }, ensure_ascii=False)
    try:
        res = ask_structured(system, user, _HINT_SCHEMA, schema_name="hint")
    except JudgeError:
        return ""
    return _normalize_hint(res.get("hint"))


def spec_template() -> dict[str, Any]:
    """Return an empty spec scaffold the model can fill in."""
    return {
        "restated_goal": "",
        "acceptance_criteria": [
            {"check": "", "evidence": ""}
        ],
        "repo_context": [
            {"cite": "", "why": ""}
        ],
        "prior_art": [],
        "risks": [],
        "non_goals": [],
        "heavy_workflow": False,
    }


# ---------------------------------------------------------------------------
# Grade contract strings
# ---------------------------------------------------------------------------

_CONTRACT: dict[str, str] = {
    "LIGHT": (
        "unifable spec contract — LIGHT grade. "
        "Before editing: drive the auto-created spec via the spec.py CLI so it carries'restated_goal' (non-empty string) "
        "and 'acceptance_criteria' (list with >=1 {check, evidence} entry). "
        "Evidence must be live command output — no placeholders."
    ),
    "STANDARD": (
        "unifable spec contract — STANDARD grade. "
        "Before editing: drive the auto-created spec via the spec.py CLI so it carries'restated_goal' "
        "and 'acceptance_criteria' (>=1 {check: <runnable command>, evidence: <live output>}). "
        "Evidence must be observed tool output, not assumed."
    ),
    "HEAVY": (
        "unifable spec contract — HEAVY grade (frontier-first workflow). "
        "Before editing: restated_goal, citation evidence, >=2 frontier approach tasks, "
        "and 1 primary approach task (blocked until frontiers ruled out). "
        "CLI: unifable set-primary / unifable add-frontier. Judge adjudicates frontiers on Stop."
    ),
}


def contract_string(grade: str, require_evidence: bool = False) -> str:
    """Return the pass-conditions for *grade* as a short additionalContext string.

    When *require_evidence* is True, append the evidence-gate citation requirements
    (repo_context with why-rationale + prior_art, both at STANDARD+).
    """
    grade = (grade or "STANDARD").upper()
    base = _CONTRACT.get(grade, _CONTRACT["STANDARD"])
    if require_evidence and grade != "LIGHT":
        base = base + (
            " Evidence gate: also include 'repo_context' (>=1 {cite:'path:line', why:'why it's "
            "relevant'}) and 'prior_art' (>=1 {cite:'http(s)://...', why:'why it backs the approach'})."
        )
    return base


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_validate(args: argparse.Namespace) -> int:
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        print(
            f"No spec found at {spec_path(args.root, args.task_id)}.",
            file=sys.stderr,
        )
        return 1
    grade = (args.grade or "STANDARD").upper()
    ok, reasons = validate_spec(spec, grade, require_evidence=getattr(args, "require_evidence", False))
    if ok:
        print(f"spec valid (grade={grade})")
        return 0
    for reason in reasons:
        print(f"- {reason}")
    return 1


def _cmd_contract(args: argparse.Namespace) -> int:
    grade = (args.grade or "STANDARD").upper()
    if grade not in GRADES:
        print(f"Unknown grade '{grade}'; expected one of {', '.join(GRADES)}.", file=sys.stderr)
        return 1
    print(contract_string(grade, getattr(args, "require_evidence", False)))
    return 0


def _next_task_id(spec: dict[str, Any]) -> str:
    return f"T{len(spec.get('tasks') or []) + 1}"


def _new_task(spec: dict[str, Any], title: str, check: str) -> dict[str, Any]:
    return {
        "id": _next_task_id(spec), "title": title.strip(), "check": check.strip(),
        "status": "pending", "exit": None, "output": "",
        "judge_verdict": None, "judge_reason": "", "judge_hint": "",
    }


def _cmd_add_task(args: argparse.Namespace) -> int:
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        # Self-heal: creation is normally the hook's job, but if the spec is
        # missing, the agent's first add-task seeds a requires_tasks scaffold
        # (goal taken from the requirement) rather than dead-ending on `create`,
        # which the agent is not allowed to run.
        spec = spec_template()
        spec["restated_goal"] = args.title.strip()
        spec["acceptance_criteria"] = []
        spec["repo_context"] = []
        spec["prior_art"] = []
        spec["tasks"] = []
        spec["requires_tasks"] = True
    spec.setdefault("tasks", [])
    task = _new_task(spec, args.title, args.check)
    spec["tasks"].append(task)
    save_spec(args.root, args.task_id, spec)
    print(f"Added {task['id']}: {task['title']}")
    notify_spec_update(
        spec,
        f"Requirement {task['id']} added: {task['title']}.",
        highlight_task=task["id"],
    )
    return 0


def _cmd_set_primary(args: argparse.Namespace) -> int:
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        print(f"No spec at {spec_path(args.root, args.task_id)}.", file=sys.stderr)
        return 1
    try:
        task = set_primary_task(spec, args.title, args.check)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_spec(args.root, args.task_id, spec)
    print(f"Primary approach set: {task['id']} (blocked until frontiers ruled out).")
    notify_spec_update(spec, f"Primary approach {task['id']} set (blocked until frontiers rejected).")
    return 0


def _cmd_add_frontier(args: argparse.Namespace) -> int:
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        print(f"No spec at {spec_path(args.root, args.task_id)}.", file=sys.stderr)
        return 1
    task = append_frontier_task(spec, args.title, args.check, added_by="agent")
    save_spec(args.root, args.task_id, spec)
    n = len(frontier_tasks(spec))
    print(f"Frontier approach added: {task['id']} ({n} total).")
    notify_spec_update(spec, f"Frontier {task['id']} added ({n}/2 for declare phase).")
    return 0


def _cmd_restate(args: argparse.Namespace) -> int:
    """Set restated_goal in the agent's own words and clear the goal_seeded marker."""
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        print(f"No spec at {spec_path(args.root, args.task_id)}.", file=sys.stderr)
        return 1
    goal = (args.goal or "").strip()
    if not goal:
        print("restate requires a non-empty goal string.", file=sys.stderr)
        return 1
    spec["restated_goal"] = goal
    spec["goal_seeded"] = False
    save_spec(args.root, args.task_id, spec)
    print(f"restated_goal set ({len(goal)} chars); goal_seeded cleared.")
    notify_spec_update(spec, "Goal restated.")
    return 0


def _cmd_dispute(args: argparse.Namespace) -> int:
    """Agent submits evidence that a requirement is impossible. This only records
    the claim (status -> disputed); the harness adjudicates on stop."""
    spec = load_spec(args.root, args.task_id)
    task = find_task(spec, args.task) if spec else None
    if task is None:
        print(f"Task {args.task} not found.", file=sys.stderr)
        return 1
    if task.get("status") == "validated":
        print(f"{args.task} is already validated; nothing to dispute.", file=sys.stderr)
        return 1
    if task.get("status") == "retracted":
        print(f"{args.task} is already retracted.", file=sys.stderr)
        return 1
    task["status"] = "disputed"
    task["dispute_evidence"] = args.evidence
    save_spec(args.root, args.task_id, spec)
    print(f"{args.task} -> disputed. The harness adjudicates impossibility claims on stop.")
    notify_spec_update(spec, f"{args.task} disputed as impossible.", highlight_task=args.task)
    return 0


def _cmd_where(args: argparse.Namespace) -> int:
    if os.environ.get("UNIFABLE_DEV", "").strip().lower() not in ("1", "true", "yes"):
        print("where is dev-only; set UNIFABLE_DEV=1.", file=sys.stderr)
        return 1
    # Always emit a machine-scannable diagnostic for the env-resolved session.
    # This line appears in Bash tool results so probes can validate whether the
    # shell subprocess receives the same session id/env as the hook/prompt scaffold.
    resolved_sid, source = resolve_session_id_with_source(default=None)
    print(f"UNIFABLE_SESSION_RESOLVED={resolved_sid or ''} SOURCE={source}", file=sys.stderr)

    print(format_spec_location(args.root, args.task_id))
    spec = load_spec(args.root, args.task_id)
    if spec is not None:
        print()
        print(format_spec_status(spec))
    else:
        fragmented = _find_fragmented_specs(args.task_id, canonical_project_root(args.root))
        if len(fragmented) > 1:
            print("\nMultiple fragmented specs found for this session (run from project root):")
            for path in fragmented:
                print(f"  {path}")
        else:
            print("\n(no spec file yet)")
    return 0


def _apply_cli_context(args: argparse.Namespace) -> int | None:
    """Resolve canonical root + session from cwd/env. Return exit code on error, else None."""
    args.root = str(canonical_project_root(os.getcwd()))
    if args.cmd == "contract":
        return None
    args.task_id = resolve_session_id(default=None)
    if args.cmd not in (None, "contract") and not args.task_id:
        print(
            "No session id: set CLAUDE_CODE_SESSION_ID, CODEX_THREAD_ID, "
            "or CURSOR_CONVERSATION_ID (Cursor).",
            file=sys.stderr,
        )
        return 1
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spec.py",
        description="unifable spec artifact validator and contract helper.",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_validate = sub.add_parser("validate", help="Validate an existing spec (harness/dev).")
    p_validate.add_argument("--grade", default="STANDARD", help="Grade tier: LIGHT, STANDARD, HEAVY.")
    p_validate.add_argument("--require-evidence", action="store_true", dest="require_evidence",
                            help="Also require citation evidence (repo_context, prior_art).")

    p_contract = sub.add_parser("contract", help="Print pass-conditions for a grade tier (harness/dev).")
    p_contract.add_argument("--grade", default="STANDARD", help="Grade tier: LIGHT, STANDARD, HEAVY.")
    p_contract.add_argument("--require-evidence", action="store_true", dest="require_evidence",
                            help="Include the evidence-gate citation requirements.")

    p_add = sub.add_parser("add-task", help="Append a task to an existing spec.")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--check", required=True, help="Runnable command that proves the task.")

    p_restate = sub.add_parser("restate", help="Restate the goal in your own words (clears goal_seeded).")
    p_restate.add_argument(
        "goal",
        help="The intended outcome, restated in your own words (quote if it contains spaces).",
    )

    p_constraint = sub.add_parser(
        "set-primary",
        help="Set the evidence-backed primary approach task (HEAVY; blocked until frontiers ruled out).",
    )
    p_constraint.add_argument("--title", required=True)
    p_constraint.add_argument("--check", required=True, help="Runnable command proving primary delivery.")

    p_rejected = sub.add_parser(
        "add-frontier",
        help="Append a frontier approach task to explore (HEAVY needs >=2).",
    )
    p_rejected.add_argument("--title", required=True)
    p_rejected.add_argument("--check", required=True, help="Runnable exploration check.")

    p_dispute = sub.add_parser(
        "dispute",
        help="Submit evidence a requirement is impossible; harness adjudicates on stop.",
    )
    p_dispute.add_argument("--task", required=True, help="Task id, e.g. T1.")
    p_dispute.add_argument("--evidence", required=True,
                           help="Proof the requirement cannot be satisfied (the judge adjudicates it).")

    sub.add_parser("where", help="Dev-only: show canonical spec path (UNIFABLE_DEV=1).")

    args = parser.parse_args(argv)
    err = _apply_cli_context(args)
    if err is not None:
        return err
    dispatch = {
        "validate": _cmd_validate, "contract": _cmd_contract,
        "restate": _cmd_restate,
        "add-task": _cmd_add_task,
        "set-primary": _cmd_set_primary,
        "add-frontier": _cmd_add_frontier,
        "dispute": _cmd_dispute,
        "where": _cmd_where,
    }
    handler = dispatch.get(args.cmd)
    if handler:
        return handler(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
