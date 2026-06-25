#!/usr/bin/env python3
"""Session/project resolution and spec artifact I/O (unifable).

Keys each evidence spec per (canonical project root, session) and reads/writes the
single spec.json atomically. Host-agnostic; re-exported by the spec.py facade.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from heavy_workflow import clear_stale_heavy_workflow
    from ledger import data_root
except ImportError:  # pragma: no cover
    from scripts.gate.heavy_workflow import clear_stale_heavy_workflow
    from scripts.gate.ledger import data_root

try:
    from spec_schema import spec_template
except ImportError:  # pragma: no cover
    from scripts.gate.spec_schema import spec_template


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
    same global root as the gate ledger). Legacy on-disk home of the spec, now
    stored in the consolidated DB; retained for one-time import and labels."""
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


def _spec_key(canonical_root: Path, session_id: str | None) -> str:
    """DB key for a session's spec: '<dirhash>/<safe_session>' (mirrors the legacy
    on-disk path segments specs/<dirhash>/<session>/spec.json)."""
    return f"{dir_hash(canonical_root)}/{_safe_session(session_id)}"


def _spec_doc_substantive(data: dict[str, Any] | None) -> bool:
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


def _find_fragmented_specs(session_id: str | None, canonical_root: Path) -> list[str]:
    """Spec keys in OTHER dirhash buckets holding a substantive spec for the same
    session. Scans the DB and, for migration, legacy on-disk spec.json fragments
    (importing each into the DB). Returns DB keys ('<dirhash>/<safe_session>');
    empty on any error."""
    safe = _safe_session(session_id)
    canonical = dir_hash(canonical_root)
    found: list[str] = []
    seen: set[str] = set()
    try:
        import db

        for key in db.spec_keys():
            if "/" not in key:
                continue
            bucket, sess = key.split("/", 1)
            if sess != safe or bucket == canonical:
                continue
            if _spec_doc_substantive(db.spec_load(key)) and key not in seen:
                found.append(key)
                seen.add(key)
        specs_root = data_root() / "specs"
        if specs_root.is_dir():
            for entry in specs_root.iterdir():
                if not entry.is_dir() or entry.name == canonical:
                    continue
                candidate = entry / safe / "spec.json"
                if not candidate.is_file():
                    continue
                doc = _read_spec_file(candidate)
                if not _spec_doc_substantive(doc):
                    continue
                key = f"{entry.name}/{safe}"
                try:
                    db.spec_save(key, doc)
                except Exception:
                    pass
                if key not in seen:
                    found.append(key)
                    seen.add(key)
    except Exception:
        return []
    return found


def _relocate_spec(from_key: str, canonical_root: Path, session_id: str | None) -> dict[str, Any] | None:
    """Move a stranded spec doc from a fragmented dirhash bucket to the canonical
    key, removing the legacy on-disk fragment. Returns the doc, or None on error."""
    try:
        import db

        doc = db.spec_load(from_key)
        if not isinstance(doc, dict):
            return None
        db.spec_save(_spec_key(canonical_root, session_id), doc)
        db.spec_delete(from_key)
    except Exception:
        return None
    # Best-effort cleanup of the legacy on-disk fragment (from migration).
    try:
        bucket, safe = from_key.split("/", 1)
        legacy = data_root() / "specs" / bucket / safe / "spec.json"
        legacy.unlink(missing_ok=True)
        parent = legacy.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            grand = parent.parent
            if grand.is_dir() and not any(grand.iterdir()):
                grand.rmdir()
    except OSError:
        pass
    print(
        f"Relocated spec from fragmented dirhash to canonical project root ({spec_path(canonical_root, session_id)}).",
        file=sys.stderr,
    )
    return doc


def _import_legacy_spec(canonical_root: Path, session_id: str | None, key: str) -> dict[str, Any] | None:
    """One-time import of a legacy spec.json file into the DB on first miss."""
    data = _read_spec_file(spec_path(canonical_root, session_id))
    if data is None:
        return None
    try:
        import db

        db.spec_save(key, data)
    except Exception:
        pass
    return data


def load_spec(cwd: str | Path, session_id: str | None) -> dict[str, Any] | None:
    """Load the session's spec doc from the consolidated DB, returning None on
    absence or any error. Falls back to importing a legacy spec.json, then to
    relocating a lone substantive spec stranded in another dirhash bucket."""
    root = canonical_project_root(cwd)
    key = _spec_key(root, session_id)
    try:
        import db

        doc = db.spec_load(key)
    except Exception:
        doc = None
    if doc is None:
        doc = _import_legacy_spec(root, session_id, key)
    if doc is not None:
        return doc
    fragmented = _find_fragmented_specs(session_id, root)
    if len(fragmented) == 1 and _git_toplevel(root) is not None:
        relocated = _relocate_spec(fragmented[0], root, session_id)
        if relocated is not None:
            return relocated
    return None


def save_spec(cwd: str | Path, session_id: str | None, spec: dict[str, Any]) -> Path:
    """Persist *spec* as one JSON doc in the consolidated DB. Returns the legacy
    spec path purely as a stable identity/label for callers and block messages."""
    root = canonical_project_root(cwd)
    try:
        import db

        db.spec_save(_spec_key(root, session_id), spec)
    except Exception:
        pass
    return spec_path(root, session_id)


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
        existing = load_spec(root, session_id)
        if existing is None:
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
            s = existing
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
            s = existing
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
