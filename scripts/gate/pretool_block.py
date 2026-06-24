#!/usr/bin/env python3
"""PreToolUse block message compression and turn-scoped deduplication.

Codex (and other hosts) may invoke PreToolUse hooks concurrently for parallel tool
calls. Without coordination each blocked call prints the full stderr message.
This module emits one full message per (epoch, signature) and silent blocks for
repeats within the same turn.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sys
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from ledger import ledger_path, load_ledger, save_ledger
except ImportError:  # pragma: no cover
    from scripts.gate.ledger import ledger_path, load_ledger, save_ledger

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

GATE_PREFIX = "unifable pre-edit gate: "

BASH_ALLOWED_SUMMARY = (
    "cd, ls, glob, rg, read-only git, git add/commit/push (no --force), "
    "trace.sh, unifusion scripts, unifable spec CLI"
)

_WHITELIST_DETAIL_RE = re.compile(
    r"^(\S+) is not in the Bash research whitelist$", re.IGNORECASE
)
_PIPELINE_DETAIL_RE = re.compile(
    r"^(\S+) is not an allowed read-only pipeline sink$", re.IGNORECASE
)


def block_epoch(input_data: dict[str, Any], ledger: dict[str, Any] | None = None) -> str:
    """Scope dedup to one assistant turn / prompt epoch."""
    turn = str(input_data.get("turn_id") or "").strip()
    if turn:
        return f"turn:{turn}"
    data = ledger if isinstance(ledger, dict) else {}
    active = data.get("active_task")
    if active:
        return f"task:{active}"
    sid = str(input_data.get("session_id") or "no-session")
    return f"session:{sid}"


def block_signature(kind: str, detail: str) -> str:
    """Stable key for dedup within an epoch."""
    kind = (kind or "other").strip().lower()
    detail = " ".join(str(detail or "").split())
    if len(detail) > 120:
        detail = hashlib.sha256(detail.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{kind}:{detail}"


def normalize_bash_detail(why: str) -> str:
    """Extract a short token from bash_classify rejection reasons."""
    text = " ".join(str(why or "").split())
    for pattern in (_WHITELIST_DETAIL_RE, _PIPELINE_DETAIL_RE):
        match = pattern.match(text)
        if match:
            return match.group(1).lower()
    if len(text) <= 80:
        return text.lower()
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def format_bash_research_block(why: str, session_id: str) -> str:
    """Compact full block for bash research-phase whitelist failures."""
    sid = (session_id or "default").strip() or "default"
    why = " ".join(str(why or "").split())
    return (
        f"Bash blocked (research phase): {why}.\n"
        "Unlock: unifable restate '<goal>' ; unifable add-task --title ... --check ... "
        "(HEAVY: set-primary, add-frontier).\n"
        f"Allowed now: {BASH_ALLOWED_SUMMARY}.\n"
        f"session-id: {sid}  (run: unifable where)"
    )


def format_delegation_block(tool_name: str, session_id: str) -> str:
    """Compact full block for Task/Agent delegation lockdown."""
    sid = (session_id or "default").strip() or "default"
    return (
        f"{tool_name} blocked before evidence spec validation (delegation bypass guard).\n"
        "Unlock: unifable restate '<goal>' ; unifable add-task --title ... --check ... "
        "(HEAVY: set-primary, add-frontier).\n"
        f"Allowed now: Read/Grep/Glob/web and Bash limited to {BASH_ALLOWED_SUMMARY}.\n"
        f"session-id: {sid}  (run: unifable where)"
    )


def format_spec_missing_block(grade: str, session_id: str, contract: str) -> str:
    """Compact full block when no evidence spec exists yet."""
    sid = (session_id or "default").strip() or "default"
    contract = " ".join(str(contract or "").split())
    return (
        f"no evidence spec for session '{sid}' (grade={grade}). "
        "Build via: unifable restate / unifable add-task "
        f"(HEAVY: set-primary, add-frontier). {contract}\n"
        f"session-id: {sid}  (run: unifable where)"
    )


@contextlib.contextmanager
def _pretool_lock(input_data: dict[str, Any]):
    """Exclusive lock for pretool block counter updates."""
    if fcntl is None:  # pragma: no cover
        yield
        return
    path = ledger_path(input_data)
    lock_path = path.parent / f"{path.name}.pretool.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _record_block_count(input_data: dict[str, Any], signature: str) -> int:
    """Increment block count for *signature* under the current epoch; return new count."""
    with _pretool_lock(input_data):
        ledger = load_ledger(input_data)
        epoch = block_epoch(input_data, ledger)
        if ledger.get("pretool_block_epoch") != epoch:
            ledger["pretool_block_epoch"] = epoch
            ledger["pretool_block_counts"] = {}
        counts = ledger.get("pretool_block_counts")
        if not isinstance(counts, dict):
            counts = {}
        count = int(counts.get(signature, 0)) + 1
        counts[signature] = count
        ledger["pretool_block_counts"] = counts
        save_ledger(input_data, ledger)
        return count


def emit_pretool_block(
    input_data: dict[str, Any],
    *,
    kind: str,
    detail: str,
    full_message: str,
) -> int:
    """Block the tool (exit 2). Print full_message only on first emission per epoch+signature."""
    message = str(full_message or "").strip()
    try:
        sig = block_signature(kind, detail)
        count = _record_block_count(input_data, sig)
        if count == 1 and message:
            print(f"{GATE_PREFIX}{message}", file=sys.stderr)
        return 2
    except Exception:
        if message:
            print(f"{GATE_PREFIX}{message}", file=sys.stderr)
        return 2
