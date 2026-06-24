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
    add-frontier / dispute / doctor session-env

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
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from atomicio import write_text_atomic
    from heavy_workflow import (
        adopted_frontier,
        advance_primary_if_ready,
        all_frontiers_rejected,
        all_frontiers_terminal,
        all_tasks_validated_heavy,
        any_frontier_accepted,
        clear_stale_heavy_workflow,
        compute_heavy_phase,
        finalize_heavy_adoption,
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
        adopted_frontier,
        advance_primary_if_ready,
        all_frontiers_rejected,
        all_frontiers_terminal,
        all_tasks_validated_heavy,
        any_frontier_accepted,
        clear_stale_heavy_workflow,
        frontier_tasks,
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
    "evidence_profile": {
        "type": str,
        "required": False,
        "description": "code | operational — set by grade classifier; operational waives repo_context/prior_art at STANDARD+.",
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

_REPO_MAINTENANCE_RE = re.compile(
    r"\b("
    r"version\s+bump|bump\s+version|just\s+version|bump_version|plugin\.json|marketplace\.json|"
    r"setup/setup\.sh|setup\.sh|manifest\s+sync|pre-commit|release(?:\s+tail|\s+workflow|\s+number)?|"
    r"tag\s+and\s+push|plugin\s+manifest"
    r")\b",
    re.I,
)
# Bounded in-repo engineering: follow existing tests/patterns; repo_context suffices.
_IN_REPO_WORK_RE = re.compile(
    r"\b("
    r"regression\s+test|"
    r"add(?:\s+an?|\s+the)?\s+(?:focused\s+|unit\s+|integration\s+)?tests?\b|"
    r"tests?/test_|"
    r"\bpytest\b|"
    r"test\s+(?:coverage|harness|suite)|"
    r"in-repo|in\s+this\s+repo|within\s+(?:the\s+)?repo|this\s+codebase|"
    r"follow(?:ing)?\s+(?:the\s+)?existing\s+(?:test|pattern|convention)s?|"
    r"extend(?:ing)?\s+(?:the\s+)?existing\s+test"
    r")\b",
    re.I,
)
_EXTERNAL_RESEARCH_RE = re.compile(
    r"\b("
    r"api\s+doc|external\s+api|third.?party|platform\s+behavior|undocumented\s+endpoint|"
    r"greenfield|new\s+architecture|migration\s+from\s+scratch"
    r")\b",
    re.I,
)


def repo_maintenance_waives_prior_art(spec: dict[str, Any]) -> bool:
    """True when prior_art is not required for bounded in-repo work.

    Covers repo maintenance (version bump, manifest sync) and engineering that
    follows existing in-repo patterns (regression tests, test additions, harness
    self-tests). External-research signals in the goal or tasks override the waiver.
    """
    if not isinstance(spec, dict):
        return False
    chunks: list[str] = [str(spec.get("restated_goal") or "")]
    for task in spec.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        chunks.append(str(task.get("title") or ""))
        chunks.append(str(task.get("check") or ""))
    combined = "\n".join(chunks)
    if _EXTERNAL_RESEARCH_RE.search(combined):
        return False
    return bool(_REPO_MAINTENANCE_RE.search(combined) or _IN_REPO_WORK_RE.search(combined))


def validate_spec(
    spec: dict[str, Any],
    grade: str,
    require_evidence: bool = False,
    evidence_profile: str | None = None,
) -> tuple[bool, list[str]]:
    """Validate *spec* against the requirements for *grade*.

    When *require_evidence* is True (how the hooks always call it), the spec must
    also carry citation evidence at STANDARD+ for the *code* profile: 'repo_context'
    (>=1 {cite: 'path:line', why: '<why relevant>'}) and 'prior_art' (>=1
    {cite: 'http(s)://...', why: '<why relevant>'}). The *operational* profile
    waives both at STANDARD+; evidence is task-driven and judged at Stop.

    Returns (ok, reasons) where reasons is empty when ok is True.
    """
    try:
        from evidence_policy import resolve_evidence_profile
    except ImportError:  # pragma: no cover
        from scripts.gate.evidence_policy import resolve_evidence_profile
    grade = (grade or "STANDARD").upper()
    if grade not in GRADES:
        return False, [f"Unknown grade '{grade}'; expected one of {', '.join(GRADES)}."]

    profile = resolve_evidence_profile(spec=spec if isinstance(spec, dict) else None)
    if evidence_profile is not None:
        profile = (evidence_profile or "").lower().strip() or profile

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
    has_tasks = isinstance(tasks, list) and any(isinstance(t, dict) and str(t.get("check", "")).strip() for t in tasks)
    criteria = spec.get("acceptance_criteria")
    if has_tasks:
        pass  # tasks stand in for acceptance_criteria
    elif spec.get("requires_tasks"):
        # Auto-created task-spec with no requirement yet: the agent must add >=1.
        reasons.append(
            "no requirements yet: add at least one with `unifable add-task --title '<req>' --check '<runnable check>'`."
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

    # Evidence gate: citation fields become required at STANDARD+ for code-profile
    # tasks (LIGHT is exempt because LIGHT waives the spec entirely upstream).
    # Operational profile waives repo_context and prior_art; task checks carry evidence.
    if require_evidence and grade in ("STANDARD", "HEAVY") and profile == "code":
        repo_context = repo_context_of(spec)  # accepts legacy `must_read` alias
        if not repo_context:
            reasons.append(
                "evidence gate: 'repo_context' is required (list, >=1 {cite: 'path:line', why: 'why this passage is relevant'})."
            )
        else:
            for idx, item in enumerate(repo_context):
                cite, why = repo_context_parts(item)
                if not is_path_line(cite):
                    reasons.append(
                        f"repo_context[{idx}].cite must be a 'path:line' code citation (e.g. src/app.py:42), got {item!r}."
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

        # prior_art — required from STANDARD up unless bounded in-repo work at STANDARD
        # (maintenance, regression tests, existing-pattern edits) where repo_context suffices.
        # HEAVY always requires prior_art for architectural exploration.
        waive_prior_art = grade != "HEAVY" and repo_maintenance_waives_prior_art(spec)
        if not waive_prior_art:
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
                        reasons.append(f"prior_art[{idx}].cite must be a source URL (http(s)://...), got {item!r}.")
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
    return f"session-id: {sid}\nproject: {root}\ndirhash: {dh} (path segment only -- not your session-id)\nspec: {path}"


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


def _seed_goal(prompt: str, limit: int = 280) -> str:
    """Best-effort restated_goal for the scaffold: the trimmed prompt. The agent
    refines it; the gate only requires a non-empty string."""
    g = " ".join((prompt or "").split())
    return g[:limit]


def ensure_spec_scaffold(
    cwd: str | Path,
    session_id: str | None,
    seed_prompt: str,
    *,
    heavy: bool = False,
    evidence_profile: str = "code",
) -> tuple[str, list[str], bool]:
    """Auto-create or update the evidence spec. Returns (spec_path, changes, created).

    Called from UserPromptSubmit (gate_prompt.py) and from ``restate`` when the
    hook did not run or failed open."""
    changes: list[str] = []
    created = False
    try:
        root = canonical_project_root(cwd)
        path = spec_path(root, session_id)
        if not path.exists():
            created = True
            s = spec_template()
            s["restated_goal"] = _seed_goal(seed_prompt)
            s["goal_seeded"] = True  # gate blocked until `unifable restate '<goal>'`
            s["acceptance_criteria"] = []
            s["repo_context"] = []
            s["prior_art"] = []
            s["tasks"] = []
            s["evidence_profile"] = evidence_profile
            s["requires_tasks"] = True  # empty spec must gain >=1 requirement to complete
            if heavy:
                s["heavy_workflow"] = True
            save_spec(root, session_id, s)
        elif heavy:
            s = load_spec(root, session_id)
            if isinstance(s, dict):
                changed = False
                if not s.get("heavy_workflow"):
                    s["heavy_workflow"] = True
                    changed = True
                    changes.append("set heavy_workflow")
                old_profile = str(s.get("evidence_profile") or "")
                if old_profile != evidence_profile:
                    s["evidence_profile"] = evidence_profile
                    changed = True
                    changes.append(f"evidence_profile {old_profile or '?'}->{evidence_profile}")
                if changed:
                    save_spec(root, session_id, s)
        else:
            s = load_spec(root, session_id)
            if isinstance(s, dict):
                changed = False
                if clear_stale_heavy_workflow(s, "STANDARD"):
                    changed = True
                    changes.append("cleared stale heavy_workflow/heavy_phase")
                old_profile = str(s.get("evidence_profile") or "")
                if old_profile != evidence_profile:
                    s["evidence_profile"] = evidence_profile
                    changed = True
                    changes.append(f"evidence_profile {old_profile or '?'}->{evidence_profile}")
                if changed:
                    save_spec(root, session_id, s)
        return str(path), changes, created
    except Exception:
        return "", [], False


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
# work was validated, the judge accepted a dispute and retracted the requirement
# as impossible, or an agent requirement was explicitly superseded by a newer one.
# Every other status (pending/delivered/failed/disputed) is open.
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
    incomplete = [str(t.get("id")) for t in tasks if not (isinstance(t, dict) and t.get("status") in RESOLVED_STATUSES)]
    return (not incomplete), incomplete


# Ceiling on the live UNRESOLVED judge-discovered backlog (not a lifetime total).
# The judge may add requirements while validating (new_requirements); bounding
# the *unresolved* judge backlog (rather than a lifetime cap) lets a legitimately
# long task keep receiving new requirements as it resolves old ones, while a
# runaway -- whose judge tasks never validate -- cannot grow its backlog past
# this. Agent-added requirements are never capped; only judge ones.
JUDGE_MAX_UNRESOLVED_ADDED = 5
# Per-check subprocess ceiling. On the Stop path auto_validate_spec further bounds
# each check by the remaining wall-clock budget so a slow check can't outlive the
# host Stop-hook timeout (the codex-thread 10s kill).
_CHECK_TIMEOUT = 600


def _apply_dispute_verdict(
    spec: dict[str, Any],
    task: dict[str, Any],
    verdict: int,
    reason: str,
) -> list[str]:
    """Apply an impossibility-dispute verdict. Mutates spec in place."""
    tid = str(task.get("id") or "")
    headlines: list[str] = []
    task["attempts"] = int(task.get("attempts") or 0) + 1
    task["judge_verdict"] = verdict
    task["judge_reason"] = reason
    task["status"] = "retracted" if verdict == 1 else "failed"
    if verdict != 1:
        notify_spec_update(
            spec,
            f"Dispute rejected for {tid}.",
            highlight_task=tid,
        )
        headlines.append(f"{tid}: dispute rejected")
    else:
        headline = f"{tid} retracted — judge accepted impossibility."
        if all_tasks_validated(spec)[0]:
            headline += " Completion breaker open."
        notify_spec_update(
            spec,
            headline,
            highlight_task=tid,
        )
        headlines.append(headline)
    return headlines


def _apply_dispute(spec: dict[str, Any], task: dict[str, Any], *, plan_mode: dict[str, Any] | None = None) -> list[str]:
    """Adjudicate a disputed task and apply the verdict. Mutates spec in place."""
    verdict, reason = judge_dispute(
        spec,
        task,
        str(task.get("dispute_evidence") or ""),
        plan_mode=plan_mode,
    )
    return _apply_dispute_verdict(spec, task, verdict, reason)


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


# Purpose-level duplicate: one normalized title extends the other (same obligation,
# extra qualifier). Requires a minimum length so "verify auth" does not match
# "verify auth token expiry in integration tests".
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
    open_statuses = frozenset({"pending", "delivered", "failed", "disputed"})
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


def _should_replay_failed_check(task: dict[str, Any]) -> bool:
    """True when a failed task should replay stored exit/output instead of re-running."""
    if str(task.get("status") or "") != "failed":
        return False
    return task.get("replay_failed") is True


def _check_inputs_for_task(
    task: dict[str, Any],
    cwd: str | Path,
    deadline: float | None,
) -> tuple[int, str]:
    """Return (exit_code, output) for judge validation.

    Open tasks get a fresh check run (bounded by the Stop wall-clock budget).
    Failed tasks replay stored output only when ``replay_failed`` is true on
    the task (escape hatch for expensive checks).
    """
    if _should_replay_failed_check(task):
        exit_code = task.get("exit")
        return (
            int(exit_code if exit_code is not None else 1),
            str(task.get("output") or ""),
        )
    if deadline is not None:
        ct = max(1, int(min(_CHECK_TIMEOUT, deadline - time.monotonic())))
        return run_check(task.get("check", ""), cwd=cwd, timeout=ct)
    return run_check(task.get("check", ""), cwd=cwd)


def _apply_check_result(
    spec: dict[str, Any],
    task: dict[str, Any],
    exit_code: int,
    output: str,
    verdict: int,
    reason: str,
    new_reqs: list[dict[str, str]],
    *,
    frontier_outcome: str = "",
    prior_exit: int | None = None,
) -> list[str]:
    """Record a check+judge outcome on the task and notify. Mutates spec in place."""
    tid = str(task.get("id") or "")
    prefix: list[str] = []
    if prior_exit is not None and prior_exit != exit_code:
        prefix.append(f"{tid} check re-run: exit {exit_code} (was {prior_exit}).")
    task["exit"] = exit_code
    task["output"] = output
    task["judge_verdict"] = verdict
    task["judge_reason"] = reason
    kind = str(task.get("approach_kind") or "requirement")
    task["attempts"] = int(task.get("attempts") or 0) + 1
    if task.get("status") == "retracted" and task.get("added_by") == "judge":
        headline = f"{tid} retracted by judge: {str(task.get('judge_reason') or reason)[:80]}"
        notify_spec_update(
            spec,
            headline,
            highlight_task=tid,
        )
        return [headline]
    added: list[str] = []
    extra_headlines: list[str] = []
    # Apply new requirements + supersedes BEFORE mutating the current task status so
    # a batch Stop can supersede sibling tasks without a later item re-failing them.
    existing_pairs = {
        (str(t.get("title") or "").strip(), str(t.get("check") or "").strip())
        for t in (spec.get("tasks") or [])
        if isinstance(t, dict)
    }
    existing_norm_titles = {_normalize_title(t.get("title")) for t in (spec.get("tasks") or []) if isinstance(t, dict)}
    judge_unresolved = sum(
        1
        for t in (spec.get("tasks") or [])
        if isinstance(t, dict) and t.get("added_by") == "judge" and t.get("status") not in RESOLVED_STATUSES
    )
    filtered_reqs = _filter_judge_new_requirements(new_reqs, existing_pairs, existing_norm_titles)
    for req in filtered_reqs:
        if judge_unresolved >= JUDGE_MAX_UNRESOLVED_ADDED:
            break
        pair = (str(req.get("title") or "").strip(), str(req.get("check") or "").strip())
        if pair in existing_pairs:
            continue
        norm_title = _normalize_title(req.get("title"))
        if norm_title and (norm_title in existing_norm_titles or _norm_title_conflicts(norm_title, existing_norm_titles)):
            continue
        spec.setdefault("tasks", [])
        nt = _new_task(spec, req["title"], req["check"])
        nt["added_by"] = "judge"
        spec["tasks"].append(nt)
        existing_pairs.add(pair)
        existing_norm_titles.add(norm_title)
        judge_unresolved += 1
        new_tid = nt["id"]
        added.append(new_tid)
        supersedes = req.get("supersedes") or []
        if isinstance(supersedes, list) and supersedes:
            extra_headlines.extend(
                _apply_supersedes_bundle(
                    spec,
                    new_tid,
                    [str(x) for x in supersedes],
                    reason=reason,
                )
            )
    if str(task.get("status") or "") in ("superseded", "retracted"):
        return extra_headlines
    if kind == "frontier":
        if frontier_outcome == "rejected_approach":
            task["status"] = "rejected_approach"
        elif frontier_outcome == "accepted_approach":
            task["status"] = "accepted_approach"
        else:
            task["status"] = "failed"
    elif kind == "primary" and adopted_frontier(spec) is not None:
        winner = adopted_frontier(spec)
        wid = str(winner.get("id") or "") if winner else ""
        task["status"] = "superseded"
        task["judge_reason"] = f"Superseded by adopted frontier {wid}."
    else:
        task["status"] = "validated" if verdict == 1 else "failed"
    advance_primary_if_ready(spec)
    sync_heavy_phase(spec)
    if kind == "frontier" and task["status"] == "accepted_approach":
        headline = f"{tid} frontier accepted by judge (check passed): {reason[:80]}."
    elif kind == "frontier" and task["status"] == "rejected_approach":
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
        spec,
        headline,
        highlight_task=tid,
    )
    return prefix + [headline] + extra_headlines


def _validate_one_task(
    spec: dict[str, Any],
    task: dict[str, Any],
    cwd: str | Path,
    *,
    transcript_path: str | None = None,
) -> list[str]:
    """Validate ONE task (dispute adjudication or check+judge). Mutates spec in place."""
    if task.get("status") == "disputed":
        _, plan_mode = _judge_context(transcript_path)
        return _apply_dispute(spec, task, plan_mode=plan_mode)
    exit_code, output = _check_inputs_for_task(task, cwd, deadline=None)
    transcript, plan_mode = _judge_context(transcript_path)
    if transcript:
        verdict, reason, new_reqs, frontier_outcome = judge_task(
            spec,
            task,
            exit_code,
            output,
            transcript=transcript,
            plan_mode=plan_mode,
        )
    else:
        verdict, reason, new_reqs, frontier_outcome = judge_task(
            spec,
            task,
            exit_code,
            output,
            plan_mode=plan_mode,
        )
    return _apply_check_result(
        spec,
        task,
        exit_code,
        output,
        verdict,
        reason,
        new_reqs,
        frontier_outcome=frontier_outcome,
    )


def auto_validate_spec(
    spec: dict[str, Any],
    cwd: str | Path,
    *,
    time_budget: float | None = None,
    transcript_path: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate every open task on stop. Mutates spec in place.

    Open tasks (including failed) get fresh checks bounded by the remaining
    wall-clock budget unless ``replay_failed`` is set on the task. Disputed
    tasks are adjudicated in the same unified judge call as
    validation tasks. One ask_structured round-trip judges all tasks together
    from shared context (goal, board, transcript, check outputs). When
    time_budget is set, check runs stop at the deadline; remaining tasks stay
    open and are picked up on the next stop."""
    messages: list[str] = []
    messages.extend(heal_judge_owned_requirements(spec, transcript_path=transcript_path))
    deadline = (time.monotonic() + time_budget) if time_budget is not None else None

    pending: list[tuple[int, dict[str, Any]]] = []
    advance_primary_if_ready(spec)
    for idx, task in enumerate(list(spec.get("tasks") or [])):
        if not isinstance(task, dict) or not _task_is_pending(task):
            continue
        pending.append((idx, task))

    if _is_heavy_spec(spec):
        pending.sort(
            key=lambda it: (
                0
                if str(it[1].get("approach_kind") or "") == "frontier"
                else 1
                if str(it[1].get("approach_kind") or "") == "primary"
                else 2,
                int(it[1].get("attempts") or 0),
                it[0],
            )
        )
    else:
        pending.sort(key=lambda it: (int(it[1].get("attempts") or 0), it[0]))
    pending_tasks = [task for _, task in pending]

    transcript, plan_mode = _judge_context(transcript_path)

    items: list[dict[str, Any]] = []
    for task in pending_tasks:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if task.get("status") == "disputed":
            items.append({"task": task, "kind": "dispute"})
            continue
        check = str(task.get("check") or "")
        # Non-runnable (prose / natural-language evidence) check: never shell-exec
        # it -- that is what produced the exit-127 "command not found" doom loop on
        # research tasks. Route it to evidence_only judging against captured tool
        # activity (reads, fetches, MCP results) + the transcript instead.
        if not is_runnable_check(check):
            items.append(
                {
                    "task": task,
                    "kind": "validate",
                    "exit_code": None,
                    "output": "",
                    "evidence_only": True,
                    "prior_exit": None,
                }
            )
            continue
        prior_exit: int | None = None
        if str(task.get("status") or "") == "failed" and not _should_replay_failed_check(task):
            raw_exit = task.get("exit")
            if raw_exit is not None:
                try:
                    prior_exit = int(raw_exit)
                except (TypeError, ValueError):
                    prior_exit = 1
        exit_code, output = _check_inputs_for_task(task, cwd, deadline)
        # Backstop: classified runnable but the shell could not find a command to
        # run (exit 127 + command-not-found) -> it is prose too; adjudicate on
        # evidence rather than recording a spurious failure.
        if exit_code == 127 and "not found" in (output or "").lower():
            items.append(
                {
                    "task": task,
                    "kind": "validate",
                    "exit_code": None,
                    "output": "",
                    "evidence_only": True,
                    "prior_exit": None,
                }
            )
            continue
        items.append(
            {
                "task": task,
                "kind": "validate",
                "exit_code": exit_code,
                "output": output,
                "prior_exit": prior_exit,
            }
        )

    if items:
        spec.pop("_stop_adjust_headlines", None)
        verdicts = judge_tasks(spec, items, transcript=transcript, plan_mode=plan_mode, evidence=evidence)
        for h in spec.pop("_stop_adjust_headlines", []):
            if h not in messages:
                messages.append(h)
        for it, (verdict, reason, new_reqs, frontier_outcome) in zip(items, verdicts):
            task = it["task"]
            if it.get("kind") == "dispute":
                messages.extend(_apply_dispute_verdict(spec, task, verdict, reason))
            else:
                revised = task.pop("_revise_this_stop", None)
                exit_code, output = it["exit_code"], it["output"]
                if task.pop("_check_stale", None):
                    exit_code, output = _check_inputs_for_task(task, cwd, deadline)
                if revised and verdict != 1:
                    task["status"] = "pending"
                    continue
                messages.extend(
                    _apply_check_result(
                        spec,
                        task,
                        exit_code,
                        output,
                        verdict,
                        reason,
                        new_reqs,
                        frontier_outcome=frontier_outcome,
                        prior_exit=it.get("prior_exit"),
                    )
                )

    # HEAVY adoption: deterministic finalization once frontiers are terminal.
    if _is_heavy_spec(spec):
        if all_frontiers_terminal(spec) and any_frontier_accepted(spec):
            adopt_headlines = finalize_heavy_adoption(spec)
            if adopt_headlines:
                messages.extend(adopt_headlines)
                notify_spec_update(spec, adopt_headlines[0])

    return spec, messages


# Command names a check may legitimately start with (shell builtins + tools that
# are not always on PATH inside the hook subprocess). A check whose first word is
# none of these AND is not resolvable via shutil.which is treated as a prose /
# evidence description, not a runnable command -- so it is adjudicated against the
# captured evidence instead of being executed and failing with exit 127. This is
# what breaks the prose-check-as-shell doom loop for research tasks.
_CHECK_BUILTINS = frozenset(
    {
        "test",
        "[",
        "[[",
        "cd",
        ":",
        "true",
        "false",
        "echo",
        "printf",
        "cat",
        "ls",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "sed",
        "awk",
        "find",
        "head",
        "tail",
        "wc",
        "diff",
        "cmp",
        "sort",
        "uniq",
        "tr",
        "cut",
        "jq",
        "yq",
        "xargs",
        "stat",
        "file",
        "touch",
        "cp",
        "mv",
        "rm",
        "mkdir",
        "python",
        "python3",
        "python2",
        "pip",
        "pip3",
        "pytest",
        "git",
        "bash",
        "sh",
        "zsh",
        "node",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "bun",
        "deno",
        "go",
        "cargo",
        "rustc",
        "make",
        "just",
        "ruff",
        "mypy",
        "pyright",
        "tsc",
        "eslint",
        "curl",
        "wget",
        "ln",
        "tee",
        "env",
    }
)
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_STRUCTURE_OPS = ("&&", "||", "|", ";", ">", "<", "$(", "`", "$((")
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def _has_shell_structure(text: str, tail_tokens: list[str]) -> bool:
    """Command-line structure that a natural-language sentence does not have:
    a shell operator anywhere, or a flag / path / file-extension token."""
    if any(op in text for op in _STRUCTURE_OPS):
        return True
    for t in tail_tokens:
        if t.startswith("-") or "/" in t or _FILE_EXT_RE.search(t):
            return True
    return False


def is_runnable_check(check: str) -> bool:
    """True when *check* is an executable shell command, False when it is prose / a
    natural-language evidence description (e.g. "Slack search returned a relevant
    direct message", "Pull request metadata shows open draft state"). Non-runnable
    checks are routed to evidence_only judging instead of being shell-executed
    (the exit-127 loop).

    A long sentence whose first word merely happens to BE a real command (`pr`,
    "PR"/"Slack") is still prose: it carries no command-line structure."""
    s = (check or "").strip()
    if not s:
        return False
    try:
        toks = shlex.split(s)
    except ValueError:
        toks = s.split()
    # Skip leading `VAR=value` env assignments to find the real command word.
    idx = 0
    while idx < len(toks) and _ENV_ASSIGN_RE.match(toks[idx]):
        idx += 1
    cmd_toks = toks[idx:]
    if not cmd_toks:
        return False
    first = cmd_toks[0]
    base = os.path.basename(first)
    first_is_command = (
        base in _CHECK_BUILTINS
        or first in _CHECK_BUILTINS
        or first.startswith(("./", "/", "~", "$", "("))
        or shutil.which(first) is not None
    )
    if not first_is_command:
        return False
    if first.startswith(("./", "/", "~", "$", "(")):
        return True
    if len(cmd_toks) == 1:
        return True
    if _has_shell_structure(s, cmd_toks[1:]):
        return True
    # Flagless multi-word command with no structure: runnable only when short
    # (`git status`, `npm test`, `make check`); a longer wordy string is prose.
    return len(cmd_toks) <= 3


def run_check(check: str, cwd: str | Path = ".", timeout: int = _CHECK_TIMEOUT) -> tuple[int, str]:
    """Run a task's check command -> (exit_code, combined stdout+stderr, capped)."""
    try:
        proc = subprocess.run(
            check,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
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
        "properties": {
            "title": {"type": "string"},
            "check": {"type": "string"},
            "supersedes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "check"],
        "additionalProperties": False,
    },
}
# Adjustments the judge may make to requirements IT previously added: retract one
# it now sees as duplicative/unsatisfiable/superseded, or revise its title/check
# (e.g. tighten a brittle literal-string check). Applied only to judge-added
# tasks; every adjustment is reported to the main model. This lets the judge
# self-correct instead of re-adding equivalent requirements each cycle.
_ADJUST_REQ_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "action": {"type": "string", "enum": ["retract", "revise"]},
            "reason": {"type": "string"},
            "title": {"type": "string"},
            "check": {"type": "string"},
        },
        "required": ["id", "action", "reason"],
        "additionalProperties": False,
    },
}
_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "integer", "enum": [0, 1]},
        "reason": {"type": "string"},
        # The judge may DISCOVER further requirements the goal needs while judging
        # this task. New ones are deduped and the unresolved backlog is capped.
        "new_requirements": _NEW_REQ_SCHEMA,
        # The judge may ADJUST requirements it itself added (retract or revise),
        # listed under existing_judge_requirements in the prompt.
        "adjust_requirements": _ADJUST_REQ_SCHEMA,
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}
_DISPUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "integer", "enum": [0, 1]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}

# Proactive nudge path only (judge_hint): verdict-free schema.
_HINT_SCHEMA = {
    "type": "object",
    "properties": {"hint": {"type": "string"}},
    "required": ["hint"],
    "additionalProperties": False,
}

_JUDGE_CORE_GUIDANCE = (
    "Before adding new_requirements, compare PURPOSE against current_requirements "
    "-- what outcome the task enforces when satisfied, not just title wording. "
    "Skip if any existing task (especially validated) already obligates the same "
    "outcome; duplicates trap completion. Include supersedes: [ids] when replacing "
    "broken checks (superseded agent tasks become non-blocking; judge tasks retract). "
    "Prefer adjust_requirements revise over adding a parallel "
    "requirement. Never add brittle literal-string or version-pinning requirements; "
    "write checks that read version fields from repo manifests and compare -- a check "
    "that fails on every version bump traps completion. Reject evidence that only "
    "grep-matches a frozen version string when the goal needs a structural manifest "
    "comparison. Judge-added tasks with broken checks must be fixed via adjust_requirements "
    "in THIS response, never by instructing the agent."
)

_JUDGE_FEEDBACK_GUIDANCE = (
    "reason is the only agent-visible feedback. On verdict=0 for agent tasks: "
    "explain why + one concrete next step (read a file, fix code, run a check). "
    "On verdict=1: brief confirmation. Never instruct the agent to fix "
    "judge-owned checks."
)

_JUDGE_HEAL_REASON_BRITTLE = "harness auto-retracted brittle version pin"

_JUDGE_HEAL_SYSTEM = (
    "You self-correct judge-added requirements the coding agent CANNOT fix. "
    "The agent has append-only spec access and cannot edit or retract judge tasks. "
    "Review judge_owned_open and return adjust_requirements ONLY (no "
    "new_requirements): action 'revise' with a runnable shell check when the check "
    "is broken, non-portable, prose, or environment-specific; action 'retract' when "
    "redundant with a validated requirement or unsatisfiable. " + _JUDGE_CORE_GUIDANCE
)
_JUDGE_HEAL_SCHEMA = {
    "type": "object",
    "properties": {
        "adjust_requirements": _ADJUST_REQ_SCHEMA,
        "reason": {"type": "string"},
    },
    "required": ["adjust_requirements"],
    "additionalProperties": False,
}

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


def _normalize_new_requirements(raw: Any) -> list[dict[str, Any]]:
    """Coerce new_requirements into [{title, check, supersedes?}]."""
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                check = str(item.get("check") or "").strip()
                if not title or not check:
                    continue
                supersedes: list[str] = []
                raw_sup = item.get("supersedes")
                if isinstance(raw_sup, list):
                    supersedes = [str(x).strip() for x in raw_sup if str(x).strip()]
                entry: dict[str, Any] = {"title": title, "check": check}
                if supersedes:
                    entry["supersedes"] = supersedes
                out.append(entry)
    return out


_JUDGE_SYSTEM = (
    "You are a strict, adversarial validator for a software task. "
    "Given the goal, one task with its check, exit code, and output: "
    "verdict 1 only if the output proves genuine completion. "
    "Be skeptical of empty output, errors, skipped tests, and mismatches. "
    "You may ADJUST requirements: 'retract' only for judge-added tasks; "
    "'revise' to fix any broken check; 'supersedes' on new_requirements to replace "
    "agent tasks. Every adjustment is reported to the agent. " + _JUDGE_CORE_GUIDANCE + " " + _JUDGE_FEEDBACK_GUIDANCE
)

_FRONTIER_JUDGE_SYSTEM = (
    "You are a strict frontier-approach adjudicator. A frontier is a realistic "
    "cutting-edge option the agent explores before falling back to the "
    "evidence-backed primary approach. "
    "Given goal, frontier title, check, exit code, and output, decide:\n"
    "- rejected_approach: evidence disqualifies this frontier.\n"
    "- still_viable: more exploration warranted.\n"
    "- accepted_approach: check passed, viable implementation path.\n"
    "Set verdict 1 when the check passed, 0 otherwise. "
    "outcome drives resolution, not verdict.\n" + _JUDGE_CORE_GUIDANCE + " " + _JUDGE_FEEDBACK_GUIDANCE
)

_PRIMARY_JUDGE_SYSTEM = (
    "You are validating delivery of the evidence-backed PRIMARY fallback approach. "
    "This task only runs when all frontier approaches were rejected (none adopted). "
    "Return verdict 1 only if the check output proves the primary approach was "
    "implemented correctly. " + _JUDGE_FEEDBACK_GUIDANCE
)

_DISCOVER_SYSTEM = (
    "You identify realistic cutting-edge frontier approaches worth exploring before "
    "committing to the evidence-backed primary fallback. Given the restated goal, "
    "current_requirements (every prior task with title and check), and recent "
    "research activity (reads, fetches), propose 0-2 frontier approaches. "
    "Each must be plausible, distinct from existing tasks AND from each other by "
    "purpose (not just wording), and testable with a runnable check command. "
    "Do not propose a frontier whose purpose duplicates an existing requirement. "
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
        "outcome": {"type": "string", "enum": ["rejected_approach", "still_viable", "accepted_approach"]},
        "reason": {"type": "string"},
        "new_requirements": _NEW_REQ_SCHEMA,
        "adjust_requirements": _ADJUST_REQ_SCHEMA,
    },
    "required": ["verdict", "outcome", "reason"],
    "additionalProperties": False,
}

_TASK_VERDICT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "verdict": {"type": "integer", "enum": [0, 1]},
        "reason": {"type": "string"},
        "outcome": {"type": "string"},
        "new_requirements": _NEW_REQ_SCHEMA,
        "adjust_requirements": _ADJUST_REQ_SCHEMA,
    },
    "required": ["id", "verdict", "reason"],
    "additionalProperties": False,
}

_VALIDATE_ALL_SCHEMA = {
    "type": "object",
    "properties": {
        "task_verdicts": {
            "type": "array",
            "items": _TASK_VERDICT_ITEM_SCHEMA,
        },
    },
    "required": ["task_verdicts"],
    "additionalProperties": False,
}

_VALIDATE_ALL_SYSTEM = (
    "You are a strict adversarial validator. Adjudicate ALL open requirements in "
    "ONE pass, plus optional session transcript context. For each entry in "
    "tasks_to_adjudicate:\n"
    "- kind=validate: does check output prove genuine completion? (standard skepticism)\n"
    "- evidence_only=true (a validate entry): the requirement has NO runnable shell "
    "check; its exit_code/output are null BY DESIGN, not a failure. Decide "
    "satisfaction SOLELY from the top-level `evidence` corpus (file reads, URL "
    "fetches, ran commands, MCP tool results) and the session transcript. Return "
    "verdict 1 when that captured evidence shows the requirement met; verdict 0 only "
    "when the evidence is absent or contradicts it. Do NOT tell the agent to convert "
    "the check into a shell command or to write a repo file -- a research "
    "requirement is proven by its retrievals, not by a grep.\n"
    "- kind=dispute: accept (verdict 1) ONLY if dispute_evidence proves impossibility.\n"
    "- approach_kind=frontier: return outcome rejected_approach, still_viable, or "
    "accepted_approach. Verdict 1 when check passed.\n"
    "- approach_kind=primary: validate primary delivery after frontiers ruled out.\n"
    "Return task_verdicts (same fields as single-task validation). " + _JUDGE_CORE_GUIDANCE + " "
    "You may ADJUST requirements via adjust_requirements on any task verdict. " + _JUDGE_FEEDBACK_GUIDANCE
)

_DISPUTE_ADJUDICATION = (
    "For kind=dispute: accept (verdict 1) ONLY if dispute_evidence genuinely "
    "proves impossibility; reject (verdict 0) if merely hard or inconvenient. "
    "When session_context.plan_mode_enabled is true, accept disputes where "
    "evidence shows the check requires repo-tracked mutation that host Plan Mode "
    "forbade for this turn."
)


_PLAN_MODE_JUDGE_RULES = (
    "When plan_mode_enabled is true: mutating repo-tracked files is forbidden. "
    "Expected deliverables: Codex <proposed_plan>; Claude ~/.claude/plans via "
    "ExitPlanMode; Cursor ~/.cursor/plans or CreatePlan output. "
    "For kind=dispute accept when evidence shows repo-file checks are impossible "
    "this turn. For kind=validate do not fail solely on missing repo files when "
    "the check targets repo output Plan Mode prevented; prefer adjust_requirements "
    "revise to a plan-based check or accept a valid dispute. "
    "Do not add new_requirements requiring repo edits while plan mode is active."
)


def _plan_mode_judge_section(plan_mode: dict[str, Any] | None) -> str:
    if not isinstance(plan_mode, dict) or not plan_mode.get("enabled"):
        return ""
    host = str(plan_mode.get("host") or "host")
    marker = str(plan_mode.get("marker") or "")
    return f"\n\n--- PLAN MODE ({host}) ---\nplan_mode_enabled: true\nmarker: {marker}\n" + _PLAN_MODE_JUDGE_RULES + "\n"


def _session_context_payload(plan_mode: dict[str, Any] | None) -> dict[str, Any]:
    pm = plan_mode if isinstance(plan_mode, dict) else {}
    return {
        "plan_mode_enabled": bool(pm.get("enabled")),
        "plan_mode_host": str(pm.get("host") or ""),
    }


_JUDGE_TRANSCRIPT_SECTION = (
    "\n\n--- SESSION TRANSCRIPT (context only; not proof) ---\n"
    "Stripped tail of the agent session: tool results, hook outputs, and "
    "conversation. Use it to understand what the model did and to avoid "
    "re-deriving requirements already satisfied in current_requirements or "
    "evidenced here. Return verdict 1 only when the check output proves the "
    "task; transcript context alone is never sufficient proof -- EXCEPT for "
    "evidence_only requirements, which have no runnable check and ARE adjudicated "
    "from the top-level evidence corpus plus this transcript.\n\n"
)


def _render_judge_transcript(transcript_path: str | None) -> str:
    """Render stripped transcript tail for requirement-validation judges."""
    tail, _pm = _judge_context(transcript_path)
    return tail


def _judge_context(transcript_path: str | None) -> tuple[str, dict[str, Any]]:
    """Stripped transcript tail plus plan-mode state from raw JSONL."""
    try:
        from plan_mode import detect_plan_mode, empty_plan_mode
    except ImportError:
        empty_plan_mode = lambda: {"enabled": False, "host": "", "marker": ""}  # noqa: E731
        detect_plan_mode = lambda _p: empty_plan_mode()  # noqa: E731
    plan_mode = detect_plan_mode(transcript_path)
    if not transcript_path:
        return "", plan_mode
    try:
        from transcript_tail import TRANSCRIPT_TOKEN_BUDGET, stripped_transcript_retained
    except ImportError:
        return "", plan_mode
    # Sticky retention (not a sliding tail): keeps a byte-identical, append-only
    # prefix across consecutive same-session Stop validations so the judge prompt
    # caches instead of busting the prefix every turn (the hottest judge path).
    return stripped_transcript_retained(transcript_path, TRANSCRIPT_TOKEN_BUDGET), plan_mode


def _judge_system_with_transcript(
    base: str,
    transcript: str,
    plan_mode: dict[str, Any] | None = None,
) -> str:
    """Append plan-mode rules and session transcript tail (tail-preserving cap)."""
    base = base + _plan_mode_judge_section(plan_mode)
    if not (transcript and transcript.strip()):
        return base
    try:
        from transcript_tail import JUDGE_EFFECTIVE_MAX_CHARS, cap_judge_message
    except ImportError:
        return base
    header = base + _JUDGE_TRANSCRIPT_SECTION
    room = max(0, JUDGE_EFFECTIVE_MAX_CHARS - len(header) - 50)
    if room < 500:
        return base
    return header + cap_judge_message(transcript.strip(), room)


def _judge_user(spec: dict[str, Any], task: dict[str, Any], exit_code: int, output: str) -> str:
    payload: dict[str, Any] = {
        "goal": spec.get("restated_goal", ""),
        "task_title": task.get("title", ""),
        "check": task.get("check", ""),
        "exit_code": exit_code,
        "output": output,
    }
    # EVERY requirement already in the spec (agent + judge, all statuses, full
    # title+check) so the judge can reason about purpose overlap before adding.
    payload["current_requirements"] = _current_requirements_payload(spec)
    # Requirements the judge itself added and may now adjust (retract/revise).
    adjustable = [
        {
            "id": str(t.get("id")),
            "title": str(t.get("title") or ""),
            "check": str(t.get("check") or ""),
            "status": str(t.get("status") or ""),
        }
        for t in (spec.get("tasks") or [])
        if isinstance(t, dict) and t.get("added_by") == "judge" and t.get("status") != "retracted"
    ]
    if adjustable:
        payload["existing_judge_requirements"] = adjustable[-20:]
    kind = str(task.get("approach_kind") or "requirement")
    if kind in ("frontier", "primary"):
        payload["approach_kind"] = kind
        primary = primary_task(spec)
        if primary:
            payload["primary_approach"] = primary.get("title", "")
    return json.dumps(payload, ensure_ascii=False)


def _normalize_adjustments(raw: Any) -> list[dict[str, str]]:
    """Coerce the judge's adjust_requirements into a clean list of
    {id, action, reason[, title][, check]}, dropping malformed or no-op entries."""
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "").strip()
        action = str(item.get("action") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not tid or action not in ("retract", "revise"):
            continue
        entry: dict[str, str] = {"id": tid, "action": action, "reason": reason}
        if action == "revise":
            title = str(item.get("title") or "").strip()
            check = str(item.get("check") or "").strip()
            if title:
                entry["title"] = title
            if check:
                entry["check"] = check
            if "title" not in entry and "check" not in entry:
                continue  # a revise with nothing to change is a no-op
        out.append(entry)
    return out


def _apply_adjustments(spec: dict[str, Any], res: Any, skip_ids: Any = ()) -> list[str]:
    """Apply judge adjust_requirements: retract judge-added tasks; revise any task
    with a broken check. skip_ids blocks retract only (revise still applies)."""
    if not isinstance(res, dict):
        return []
    adjustments = _normalize_adjustments(res.get("adjust_requirements"))
    if not adjustments:
        return []
    skip = {str(s) for s in (skip_ids or ())}
    by_id = {str(t.get("id")): t for t in (spec.get("tasks") or []) if isinstance(t, dict)}
    headlines: list[str] = []
    for adj in adjustments:
        tid = adj["id"]
        t = by_id.get(tid)
        if t is None or str(t.get("status") or "") in ("retracted", "superseded"):
            continue
        reason = adj.get("reason", "")
        if adj["action"] == "retract":
            if tid in skip:
                continue
            if t.get("added_by") != "judge":
                continue
            t["status"] = "retracted"
            t["judge_reason"] = reason
            headline = f"Judge retracted {tid}: {reason[:80]}"
        else:  # revise: fix broken checks on agent or judge tasks
            if "title" in adj:
                t["title"] = adj["title"]
            if "check" in adj:
                t["check"] = adj["check"]
            t["status"] = "pending"
            t["exit"] = None
            t["output"] = ""
            t["judge_verdict"] = None
            t["judge_reason"] = reason
            t["_check_stale"] = True
            t["_revise_this_stop"] = True
            who = "Judge" if t.get("added_by") == "judge" else "Agent req"
            headline = f"{who} requirement {tid} revised: {reason[:80]}"
        notify_spec_update(spec, headline, highlight_task=tid)
        headlines.append(headline)
    return headlines


def _judge_owned_open_tasks(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Open judge-added tasks the agent cannot retract or revise."""
    out: list[dict[str, Any]] = []
    for t in spec.get("tasks") or []:
        if not isinstance(t, dict):
            continue
        if t.get("added_by") != "judge":
            continue
        if str(t.get("status") or "") in RESOLVED_STATUSES:
            continue
        out.append(t)
    return out


def deterministic_heal_judge_requirements(spec: dict[str, Any]) -> list[str]:
    """Harness-owned fixes for judge tasks the agent cannot resolve."""
    adjustments: list[dict[str, str]] = []
    for t in _judge_owned_open_tasks(spec):
        tid = str(t.get("id") or "")
        if not tid:
            continue
        title = str(t.get("title") or "")
        check = str(t.get("check") or "")
        if is_brittle_version_pinned_requirement(title, check):
            adjustments.append(
                {
                    "id": tid,
                    "action": "retract",
                    "reason": _JUDGE_HEAL_REASON_BRITTLE,
                }
            )
    if not adjustments:
        return []
    return _apply_adjustments(spec, {"adjust_requirements": adjustments})


def judge_heal_own_requirements(
    spec: dict[str, Any],
    *,
    transcript_path: str | None = None,
) -> list[str]:
    """Judge-only pass: revise or retract open judge-added tasks. Fail-open."""
    open_tasks = _judge_owned_open_tasks(spec)
    if not open_tasks:
        return []
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError:
        return []
    transcript, plan_mode = _judge_context(transcript_path)
    payload = {
        "goal": spec.get("restated_goal", ""),
        "current_requirements": _current_requirements_payload(spec),
        "session_context": _session_context_payload(plan_mode),
        "judge_owned_open": [
            {
                "id": str(t.get("id") or ""),
                "title": str(t.get("title") or ""),
                "check": str(t.get("check") or ""),
                "status": str(t.get("status") or ""),
                "judge_reason": str(t.get("judge_reason") or ""),
                "exit": t.get("exit"),
                "output": str(t.get("output") or "")[:1500],
                "attempts": int(t.get("attempts") or 0),
                "approach_kind": str(t.get("approach_kind") or ""),
            }
            for t in open_tasks
        ],
    }
    try:
        res = ask_structured(
            _judge_system_with_transcript(_JUDGE_HEAL_SYSTEM, transcript, plan_mode),
            json.dumps(payload, ensure_ascii=False),
            _JUDGE_HEAL_SCHEMA,
            schema_name="judge_heal",
        )
    except JudgeError:
        return []
    return _apply_adjustments(spec, res)


def heal_judge_owned_requirements(
    spec: dict[str, Any],
    *,
    transcript_path: str | None = None,
) -> list[str]:
    """Self-heal judge-owned requirements before Stop validation."""
    headlines = deterministic_heal_judge_requirements(spec)
    try:
        from heavy_workflow import advance_primary_if_ready, sync_heavy_phase

        if sync_heavy_phase(spec):
            pass
        advance_primary_if_ready(spec)
    except Exception:
        pass
    if _judge_owned_open_tasks(spec):
        headlines.extend(judge_heal_own_requirements(spec, transcript_path=transcript_path))
        try:
            from heavy_workflow import advance_primary_if_ready, sync_heavy_phase

            sync_heavy_phase(spec)
            advance_primary_if_ready(spec)
        except Exception:
            pass
    return headlines


def _judge_system_for_task(
    task: dict[str, Any],
    *,
    transcript: str = "",
    plan_mode: dict[str, Any] | None = None,
) -> str:
    kind = str(task.get("approach_kind") or "requirement")
    if kind == "frontier":
        base = _FRONTIER_JUDGE_SYSTEM
    elif kind == "primary":
        base = _PRIMARY_JUDGE_SYSTEM
    else:
        base = _JUDGE_SYSTEM
    return _judge_system_with_transcript(base, transcript, plan_mode=plan_mode)


def _judge_schema_for_task(task: dict[str, Any]) -> dict[str, Any]:
    kind = str(task.get("approach_kind") or "requirement")
    if kind == "frontier":
        return _FRONTIER_JUDGE_SCHEMA
    return _JUDGE_SCHEMA


def _judge_result(res: Any, task: dict[str, Any] | None = None) -> tuple[int, str, list[dict[str, str]], str]:
    verdict = 1 if isinstance(res, dict) and res.get("verdict") == 1 else 0
    reason = str(res.get("reason") or "") if isinstance(res, dict) else ""
    new_reqs = _normalize_new_requirements(res.get("new_requirements")) if isinstance(res, dict) else []
    frontier_outcome = ""
    if task and str(task.get("approach_kind") or "") == "frontier" and isinstance(res, dict):
        outcome = str(res.get("outcome") or "").strip()
        if outcome in ("rejected_approach", "still_viable", "accepted_approach"):
            frontier_outcome = outcome
        verdict = 1 if outcome == "accepted_approach" else 0
    return verdict, reason, new_reqs, frontier_outcome


def _evidence_payload(evidence: dict[str, Any] | None) -> dict[str, list[str]] | None:
    """Bounded captured-activity corpus for the judge: file reads, URL fetches,
    Bash commands, and MCP tool results. This is the proof an evidence_only
    (research) requirement is adjudicated against."""
    if not isinstance(evidence, dict):
        return None

    def _take(key: str, n: int) -> list[str]:
        return [str(x) for x in (evidence.get(key) or []) if str(x)][-n:]

    out = {
        "read_paths": _take("read_paths", 30),
        "fetched_urls": _take("fetched_urls", 20),
        "ran_commands": _take("ran_commands", 20),
        "tool_results": _take("tool_evidence", 30),
    }
    return out if any(out.values()) else None


def _build_validate_all_user(
    spec: dict[str, Any],
    items: list[dict[str, Any]],
    plan_mode: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> str:
    """Build the unified validation payload for all open tasks."""
    tasks_payload: list[dict[str, Any]] = []
    for it in items:
        task = it["task"]
        entry: dict[str, Any] = {
            "id": str(task.get("id") or ""),
            "title": str(task.get("title") or ""),
            "check": str(task.get("check") or ""),
            "status": str(task.get("status") or ""),
            "kind": str(it.get("kind") or "validate"),
        }
        kind = str(task.get("approach_kind") or "")
        if kind:
            entry["approach_kind"] = kind
        if it.get("kind") == "dispute":
            entry["dispute_evidence"] = str(task.get("dispute_evidence") or "")
        else:
            # evidence_only: the check is prose / not a runnable command, so there
            # is no exit code to weigh. Adjudicate from the evidence corpus below.
            if it.get("evidence_only"):
                entry["evidence_only"] = True
                entry["exit_code"] = None
                entry["output"] = ""
            else:
                entry["exit_code"] = it.get("exit_code")
                entry["output"] = str(it.get("output") or "")
        tasks_payload.append(entry)
    payload: dict[str, Any] = {
        "goal": spec.get("restated_goal", ""),
        "current_requirements": _current_requirements_payload(spec),
        "tasks_to_adjudicate": tasks_payload,
        "session_context": _session_context_payload(plan_mode),
    }
    ev = _evidence_payload(evidence)
    if ev:
        payload["evidence"] = ev
    adjustable = [
        {
            "id": str(t.get("id")),
            "title": str(t.get("title") or ""),
            "check": str(t.get("check") or ""),
            "status": str(t.get("status") or ""),
        }
        for t in (spec.get("tasks") or [])
        if isinstance(t, dict) and t.get("added_by") == "judge" and t.get("status") != "retracted"
    ]
    if adjustable:
        payload["existing_judge_requirements"] = adjustable[-20:]
    primary = primary_task(spec)
    if primary:
        payload["primary_approach"] = primary.get("title", "")
    return json.dumps(payload, ensure_ascii=False)


def _validate_all_system(transcript: str, plan_mode: dict[str, Any] | None = None) -> str:
    base = _VALIDATE_ALL_SYSTEM + " " + _DISPUTE_ADJUDICATION
    return _judge_system_with_transcript(base, transcript, plan_mode=plan_mode)


# ---------------------------------------------------------------------------
# Frontier comparison round (adoption path)
# ---------------------------------------------------------------------------

_COMPARISON_SYSTEM = (
    "You are a senior engineer comparing frontier approaches that were ALL explored. "
    "You receive the goal and every frontier's title, check, exit code, output, and "
    "prior judge reasoning. Your job is to select the SINGLE best frontier -- the one "
    "with the strongest empirical evidence (passing checks, better output quality, "
    "more robust approach, closer fit to the goal). You MUST select one when any "
    "frontier has accepted_approach status. Provide selection_rationale explaining "
    "WHY the winner was chosen over the others, citing specific evidence from the "
    "frontier results (exit codes, output characteristics, approach trade-offs). "
    "If NO frontier has accepted_approach status, return selected_id as null "
    "(the primary fallback will be used instead)."
)

_COMPARISON_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_id": {"type": ["string", "null"]},
        "selection_rationale": {"type": "string"},
    },
    "required": ["selected_id", "selection_rationale"],
    "additionalProperties": False,
}


def judge_frontier_comparison(spec: dict[str, Any]) -> list[str]:
    """Compare all explored frontiers and select the best. Returns headlines.

    Called after all task verdicts are applied in auto_validate_spec when
    all_frontiers_terminal(spec) is True and at least one frontier has
    accepted_approach status. Reads persisted evidence (exit, output,
    judge_reason) from each frontier task -- no new persistence layer."""
    frontiers = frontier_tasks(spec)
    accepted = [t for t in frontiers if str(t.get("status") or "") == "accepted_approach"]
    if not accepted:
        return []

    payload = {
        "goal": spec.get("restated_goal", ""),
        "frontiers": [
            {
                "id": str(t.get("id")),
                "title": str(t.get("title") or ""),
                "check": str(t.get("check") or ""),
                "exit_code": t.get("exit"),
                "output": str(t.get("output") or "")[:2000],
                "judge_reason": str(t.get("judge_reason") or ""),
                "status": str(t.get("status") or ""),
            }
            for t in frontiers
        ],
    }
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError:
        return []
    try:
        res = ask_structured(
            _COMPARISON_SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            _COMPARISON_SCHEMA,
            schema_name="frontier_comparison",
        )
    except JudgeError:
        return []

    selected_id = str(res.get("selected_id") or "").strip()
    rationale = str(res.get("selection_rationale") or "").strip()

    headlines: list[str] = []
    primary = primary_task(spec)
    for t in frontiers:
        tid = str(t.get("id"))
        if tid == selected_id:
            t["comparison_winner"] = True
            t["judge_reason"] = f"Selected as best approach: {rationale[:200]}"
            headlines.append(f"{tid} selected as best frontier: {rationale[:80]}.")
        elif str(t.get("status") or "") == "accepted_approach":
            t["status"] = "rejected_approach"
            t["comparison_winner"] = False
            headlines.append(f"{tid} not selected in comparison.")

    if primary and str(primary.get("status") or "") == "blocked":
        primary["status"] = "superseded"
        primary["judge_reason"] = f"Superseded by adopted frontier {selected_id}."
        headlines.append(f"Primary superseded by adopted frontier {selected_id}.")

    sync_heavy_phase(spec)
    advance_primary_if_ready(spec)
    return headlines


def judge_all_tasks(
    spec: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    transcript: str = "",
    plan_mode: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> list[tuple[int, str, list[dict[str, str]], str]]:
    """Judge every open task in ONE structured call from shared session context.

    Judge retract/revise headlines from adjust_requirements are stashed on
    ``spec["_stop_adjust_headlines"]`` so auto_validate_spec can merge them into
    the Stop digest -- otherwise they are applied to the spec but lost to the
    model. The stash is transient and drained + popped by auto_validate_spec; it
    avoids threading a new kwarg through the judge seam that tests stub."""
    if not items:
        return []
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError as exc:  # pragma: no cover
        return [(0, f"judge unavailable: {exc}", [], "") for _ in items]
    try:
        res = ask_structured(
            _validate_all_system(transcript, plan_mode),
            _build_validate_all_user(spec, items, plan_mode, evidence),
            _VALIDATE_ALL_SCHEMA,
            schema_name="validate_all",
        )
    except JudgeError as exc:
        return [(0, f"judge error: {exc}", [], "") for _ in items]
    raw_verdicts = res.get("task_verdicts") if isinstance(res, dict) else None
    by_id: dict[str, dict[str, Any]] = {}
    if isinstance(raw_verdicts, list):
        for v in raw_verdicts:
            if isinstance(v, dict) and v.get("id") is not None:
                by_id[str(v.get("id"))] = v
    judged_ids = {str(it["task"].get("id")) for it in items}
    out: list[tuple[int, str, list[dict[str, str]], str]] = []
    for it in items:
        tid = str(it["task"].get("id"))
        v = by_id.get(tid)
        if not v:
            out.append((0, f"judge omitted task {tid}", [], ""))
            continue
        skip = set(judged_ids)
        if it["task"].get("added_by") == "judge":
            skip.discard(tid)
        hl = _apply_adjustments(spec, v, skip_ids=skip)
        if hl:
            spec.setdefault("_stop_adjust_headlines", []).extend(hl)
        out.append(_judge_result(v, it["task"]))
    return out


def judge_task(
    spec: dict[str, Any],
    task: dict[str, Any],
    exit_code: int,
    output: str,
    *,
    transcript: str = "",
    plan_mode: dict[str, Any] | None = None,
) -> tuple[int, str, list[dict[str, str]], str]:
    """Ask the judge whether a single check output validates the task.

    Returns (verdict, reason, new_requirements, frontier_outcome).
    frontier_outcome is 'rejected_approach' or 'still_viable' for frontier tasks."""
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError as exc:  # pragma: no cover
        return 0, f"judge unavailable: {exc}", [], ""
    try:
        res = ask_structured(
            _judge_system_for_task(task, transcript=transcript, plan_mode=plan_mode),
            _judge_user(spec, task, exit_code, output),
            _judge_schema_for_task(task),
            schema_name="task_verdict",
        )
    except JudgeError as exc:
        return 0, f"judge error: {exc}", [], ""
    # skip_ids blocks retract on listed ids only; revise always allowed.
    _apply_adjustments(spec, res, skip_ids=set())
    return _judge_result(res, task)


def judge_tasks(
    spec: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    transcript: str = "",
    plan_mode: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> list[tuple[int, str, list[dict[str, str]], str]]:
    """Judge all items in one unified structured call (validate + dispute)."""
    return judge_all_tasks(spec, items, transcript=transcript, plan_mode=plan_mode, evidence=evidence)


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
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError:
        return []
    user = json.dumps(
        {
            "goal": spec.get("restated_goal", ""),
            "existing_frontiers": [t.get("title") for t in frontier_tasks(spec)],
            "current_requirements": _current_requirements_payload(spec),
            "read_paths": (recent_activity.get("read_paths") or [])[-20:],
            "fetched_urls": (recent_activity.get("fetched_urls") or [])[-10:],
            "repo_context": spec.get("repo_context") or [],
            "prior_art": spec.get("prior_art") or [],
        },
        ensure_ascii=False,
    )
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
            spec,
            title,
            check,
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


def judge_dispute(
    spec: dict[str, Any],
    task: dict[str, Any],
    evidence: str,
    *,
    plan_mode: dict[str, Any] | None = None,
) -> tuple[int, str]:
    """Adjudicate an agent's claim that a requirement is IMPOSSIBLE.

    The agent has submitted `evidence` that the task cannot be satisfied. Return
    (verdict, reason): verdict 1 accepts the impossibility (the caller retracts the
    requirement), 0 rejects it (the requirement stays open with feedback). A judge
    failure returns (0, reason) so an unreachable judge never auto-retracts a
    requirement -- impossibility must be earned, not granted by default."""
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
    except ImportError as exc:  # pragma: no cover
        return 0, f"judge unavailable: {exc}"
    system = (
        "You are a strict adjudicator. An agent claims a REQUIRED task is impossible "
        "and submits evidence. Accept (verdict 1) ONLY if the evidence genuinely "
        "proves the task cannot be done -- a real, demonstrated blocker, not a "
        "preference, a difficulty, or an excuse. Reject (verdict 0) if the evidence "
        "is weak, the task is merely hard or inconvenient, or the agent is dodging "
        "work; in reason, tell the agent bluntly what real proof would be required. "
        "Do not accept a claim that work is 'complete' here -- this is only about "
        "whether the requirement is genuinely impossible. " + _JUDGE_FEEDBACK_GUIDANCE
    )
    system += _plan_mode_judge_section(plan_mode)
    user = json.dumps(
        {
            "goal": spec.get("restated_goal", ""),
            "task_title": task.get("title", ""),
            "check": task.get("check", ""),
            "impossibility_evidence": evidence,
            "current_requirements": _current_requirements_payload(spec),
            "session_context": _session_context_payload(plan_mode),
        },
        ensure_ascii=False,
    )
    try:
        res = ask_structured(system, user, _DISPUTE_SCHEMA, schema_name="dispute_verdict")
    except JudgeError as exc:
        return 0, f"judge error: {exc}"
    return (
        1 if res.get("verdict") == 1 else 0,
        str(res.get("reason") or ""),
    )


def judge_hint(spec: dict[str, Any], *, signal: str, recent: str = "") -> str:
    """Proactive, verdict-free nudge for an agent that looks stuck or is wandering.

    Unlike judge_task/judge_dispute, this renders NO verdict and resolves NO task
    -- it returns advisory guidance only. Callers (the Stop completion-breaker loop
    and the PostToolUse repeated-failure loop) surface the returned string on a
    clearly-advisory channel; it can never lift a gate. Any judge failure returns
    "" so a hint never blocks and an unreachable judge is simply silent."""
    try:
        from codex_judge import JudgeError
        from judge_transport import ask_structured
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
    user = json.dumps(
        {
            "goal": spec.get("restated_goal", ""),
            "why_it_looks_stuck": signal,
            "tasks": [
                {"id": t.get("id"), "title": t.get("title"), "status": t.get("status"), "judge_reason": t.get("judge_reason")}
                for t in board
                if isinstance(t, dict)
            ],
            "recent_activity": recent[:2000],
        },
        ensure_ascii=False,
    )
    try:
        res = ask_structured(system, user, _HINT_SCHEMA, schema_name="hint")
    except JudgeError:
        return ""
    return _normalize_hint(res.get("hint"))


def spec_template() -> dict[str, Any]:
    """Return an empty spec scaffold the model can fill in."""
    return {
        "restated_goal": "",
        "acceptance_criteria": [{"check": "", "evidence": ""}],
        "repo_context": [{"cite": "", "why": ""}],
        "prior_art": [],
        "evidence_profile": "code",
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
        "unifable spec contract — HEAVY grade (frontier-first workflow with adoption). "
        "Before editing: restated_goal, citation evidence, >=2 frontier approach tasks, "
        "and 1 primary approach task. Judge adjudicates frontiers on Stop "
        "(rejected_approach / still_viable / accepted_approach). When all frontiers "
        "are explored, the judge compares evidence and may adopt the best frontier "
        "(primary is superseded) or fall back to primary if none accepted. "
        "CLI: unifable set-primary / unifable add-frontier."
    ),
}


def contract_string(
    grade: str,
    require_evidence: bool = False,
    evidence_profile: str | None = None,
    spec: dict[str, Any] | None = None,
) -> str:
    """Return the pass-conditions for *grade* as a short additionalContext string.

    When *require_evidence* is True, append the evidence-gate citation requirements
    (code profile: repo_context + prior_art at STANDARD+; operational: tasks only).
    """
    try:
        from evidence_policy import DEFAULT_EVIDENCE_PROFILE, resolve_evidence_profile
    except ImportError:  # pragma: no cover
        from scripts.gate.evidence_policy import DEFAULT_EVIDENCE_PROFILE

    grade = (grade or "STANDARD").upper()
    base = _CONTRACT.get(grade, _CONTRACT["STANDARD"])
    profile = (evidence_profile or DEFAULT_EVIDENCE_PROFILE).lower().strip()
    if profile not in ("code", "operational"):
        profile = DEFAULT_EVIDENCE_PROFILE
    if require_evidence and grade != "LIGHT":
        if profile == "operational":
            base = base + (
                " Evidence gate (operational): restated goal + >=1 requirement task; "
                "no repo path:line or external URL required before edits -- task "
                "checks are judged at Stop."
            )
        else:
            if isinstance(spec, dict) and repo_maintenance_waives_prior_art(spec):
                base = base + (
                    " Evidence gate (in-repo): include 'repo_context' (>=1 "
                    "{cite:'path:line', why:'why it's relevant'}) from code you read; "
                    "external prior_art is not required for bounded in-repo work "
                    "(maintenance, regression tests, patterns from existing code)."
                )
            else:
                base = base + (
                    " Evidence gate: also include 'repo_context' (>=1 {cite:'path:line', why:'why it's "
                    "relevant'}) and 'prior_art' (>=1 {cite:'http(s)://...', why:'why it backs the approach'})."
                )
    return base


def format_spec_validation_block(
    grade: str,
    reasons: list[str],
    evidence_profile: str | None = None,
    spec: dict[str, Any] | None = None,
    *,
    include_contract: bool = True,
) -> str:
    """Model-facing block text when validate_spec fails.

    Omits filesystem paths (the model drives the spec via CLI and activity sync,
    not by editing spec.json). Appends concrete fix steps derived from *reasons*.
    """
    grade = (grade or "STANDARD").upper()
    items = [str(r).strip() for r in (reasons or []) if str(r).strip()]
    joined = " ".join(items).lower()
    fixes: list[str] = []

    if "prior_art" in joined:
        fixes.append(
            "fetch at least one relevant source URL (WebFetch or curl); prior_art entries sync from fetched URLs automatically"
        )
    if "repo_context" in joined:
        fixes.append("read relevant repo files (Read/Grep); repo_context entries sync from reads automatically")
    if "restate" in joined or "restated_goal" in joined or "goal_seeded" in joined:
        fixes.append("run `unifable restate '<goal in your own words>'`")
    if "no requirements yet" in joined or "requires_tasks" in joined:
        fixes.append("run `unifable add-task --title '<requirement>' --check '<runnable check>'`")
    if grade == "HEAVY" and ("frontier" in joined or "primary approach" in joined):
        fixes.append("HEAVY: use `unifable add-frontier` (>=2) and `unifable set-primary`")

    lines = [f"Evidence spec does not satisfy grade {grade}:"]
    lines.extend(f"  {item}" for item in items)
    if fixes:
        lines.append("")
        lines.append("To unblock edits:")
        lines.extend(f"  {fix}" for fix in fixes)
    else:
        lines.append("")
        lines.append("Fix the spec via the unifable CLI (never edit spec.json directly).")
    if include_contract:
        lines.append("")
        lines.append(contract_string(grade, True, evidence_profile, spec))
    return "\n".join(lines)


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
    goal = (args.goal or "").strip()
    if not goal:
        print("restate requires a non-empty goal string.", file=sys.stderr)
        return 1
    spec = load_spec(args.root, args.task_id)
    created = False
    if spec is None:
        path, _, created = ensure_spec_scaffold(args.root, args.task_id, goal)
        if not path:
            print(f"Could not create spec at {spec_path(args.root, args.task_id)}.", file=sys.stderr)
            return 1
        spec = load_spec(args.root, args.task_id)
        if spec is None:
            print(f"No spec at {spec_path(args.root, args.task_id)}.", file=sys.stderr)
            return 1
    spec["restated_goal"] = goal
    spec["goal_seeded"] = False
    save_spec(args.root, args.task_id, spec)
    if created:
        print(
            f"spec created at {spec_path(args.root, args.task_id)}; restated_goal set ({len(goal)} chars); goal_seeded cleared."
        )
    else:
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


def _cmd_doctor_session_env(args: argparse.Namespace) -> int:
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


def _cmd_doctor(args: argparse.Namespace) -> int:
    if getattr(args, "doctor_cmd", "") == "session-env":
        return _cmd_doctor_session_env(args)
    print("usage: unifable doctor session-env", file=sys.stderr)
    return 1


def _apply_cli_context(args: argparse.Namespace) -> int | None:
    """Resolve canonical root + session from cwd/env. Return exit code on error, else None."""
    args.root = str(canonical_project_root(os.getcwd()))
    if args.cmd == "contract":
        return None
    args.task_id = resolve_session_id(default=None)
    if args.cmd not in (None, "contract") and not args.task_id:
        print(
            "No session id: set CLAUDE_CODE_SESSION_ID, CODEX_THREAD_ID, or CURSOR_CONVERSATION_ID (Cursor).",
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
    p_validate.add_argument(
        "--require-evidence",
        action="store_true",
        dest="require_evidence",
        help="Also require citation evidence (repo_context, prior_art).",
    )

    p_contract = sub.add_parser("contract", help="Print pass-conditions for a grade tier (harness/dev).")
    p_contract.add_argument("--grade", default="STANDARD", help="Grade tier: LIGHT, STANDARD, HEAVY.")
    p_contract.add_argument(
        "--require-evidence",
        action="store_true",
        dest="require_evidence",
        help="Include the evidence-gate citation requirements.",
    )

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
    p_dispute.add_argument(
        "--evidence", required=True, help="Proof the requirement cannot be satisfied (the judge adjudicates it)."
    )

    p_doctor = sub.add_parser("doctor", help="Operator diagnostics.")
    doctor_sub = p_doctor.add_subparsers(dest="doctor_cmd")
    doctor_sub.add_parser("session-env", help="Show canonical spec path and session env diagnostics.")

    args = parser.parse_args(argv)
    err = _apply_cli_context(args)
    if err is not None:
        return err
    dispatch = {
        "validate": _cmd_validate,
        "contract": _cmd_contract,
        "restate": _cmd_restate,
        "add-task": _cmd_add_task,
        "set-primary": _cmd_set_primary,
        "add-frontier": _cmd_add_frontier,
        "dispute": _cmd_dispute,
        "doctor": _cmd_doctor,
    }
    handler = dispatch.get(args.cmd)
    if handler:
        return handler(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
