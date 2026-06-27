#!/usr/bin/env python3
"""PostToolUse additionalContext dedup and ledger-backed guidance cache."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
from typing import Any

try:
    from ledger import emit_json, load_ledger, save_ledger
    from pretool_block import block_epoch
except ImportError:  # pragma: no cover
    from scripts.gate.ledger import emit_json, load_ledger, save_ledger
    from scripts.gate.pretool_block import block_epoch

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def _body_hash(body: str) -> str:
    return hashlib.sha256(str(body or "").encode("utf-8", "replace")).hexdigest()


@contextlib.contextmanager
def _posttool_lock(input_data: dict[str, Any]):
    if fcntl is None:  # pragma: no cover
        yield
        return
    try:
        from ledger import ledger_path
    except ImportError:
        from scripts.gate.ledger import ledger_path  # pragma: no cover

    path = ledger_path(input_data)
    lock_path = path.parent / f"{path.name}.posttool.lock"
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


def filter_breaker_status(ledger: dict[str, Any], status_line: str) -> str:
    """Omit unchanged standing breaker status lines."""
    line = str(status_line or "").strip()
    if not line:
        return ""
    if line == str(ledger.get("posttool_last_breaker_status") or "").strip():
        return ""
    return line


def filter_failure_hint(ledger: dict[str, Any], hint: str, failure_sig: str) -> str:
    """One failure-class hint per signature per epoch."""
    text = str(hint or "").strip()
    if not text:
        return ""
    digest = _body_hash(f"{failure_sig}:{text}")
    if digest == str(ledger.get("posttool_last_failure_hint_hash") or ""):
        return ""
    return text


def compact_discovery_context(ledger: dict[str, Any], full_context: str) -> str:
    """After first frontier-discovery board, emit headline only."""
    text = str(full_context or "").strip()
    if not text:
        return ""
    first = text.split("\n", 1)[0].strip()
    last = str(ledger.get("posttool_last_discovery_headline") or "").strip()
    if last and last == first:
        return ""
    if "\n" in text and last:
        return first
    return text


_SPECUPDATE_HEADLINE_RE = re.compile(
    r"^\s*(Judge (?:retracted|added)|T\d+ revised|.*?\bsuperseded by\b.*)",
    re.IGNORECASE,
)
_SPECUPDATE_TID_RE = re.compile(r"\bT\d+\b")


def _specupdate_signature(part: str) -> str:
    """Structural identity of a 'Spec update:' block: the (sorted task-ids, action
    verbs) it touches, IGNORING the free-text reason. Two reconcile injections that
    revise/retract the same tasks with the same verbs collapse to one signature even
    when the judge paraphrases the reason every turn -- which a full-body hash misses.
    Returns "" when the block carries no recognizable headline (caller keeps it)."""
    keys: list[str] = []
    for line in str(part or "").splitlines():
        m = _SPECUPDATE_HEADLINE_RE.match(line)
        if not m:
            continue
        verb = " ".join(m.group(1).lower().split())
        verb = re.sub(r"\bt\d+\b", "", verb).strip()
        tids = ",".join(sorted(set(_SPECUPDATE_TID_RE.findall(line))))
        keys.append(f"{verb}|{tids}")
    if not keys:
        return ""
    joined = "\n".join(sorted(keys))
    return hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()


def filter_spec_update(ledger: dict[str, Any], part: str) -> str:
    """Drop a 'Spec update:' block whose structural signature (tasks + actions)
    already surfaced this epoch -- the cosmetic-reword churn guard. Fail-open: an
    unrecognizable block (empty signature) is always kept."""
    sig = _specupdate_signature(part)
    if not sig:
        return part
    if sig == str(ledger.get("posttool_last_specupdate_sig") or ""):
        return ""
    return part


def prepare_posttool_parts(
    input_data: dict[str, Any],
    parts: list[str],
    *,
    failure_sig: str = "",
) -> tuple[list[str], dict[str, str]]:
    """Filter repetitive PostToolUse parts; return updates for ledger cache."""
    updates: dict[str, str] = {}
    try:
        ledger = load_ledger(input_data)
    except Exception:
        return [p for p in parts if p and str(p).strip()], updates

    out: list[str] = []
    for raw in parts:
        part = str(raw or "").strip()
        if not part:
            continue
        if part.startswith("breaker: ARMED") or part.startswith("breaker: PROVISIONAL"):
            filtered = filter_breaker_status(ledger, part)
            if filtered:
                out.append(filtered)
                updates["posttool_last_breaker_status"] = filtered
            continue
        if part.startswith("Hint: "):
            filtered = filter_failure_hint(ledger, part[6:], failure_sig)
            if filtered:
                out.append(f"Hint: {filtered}")
                updates["posttool_last_failure_hint_hash"] = _body_hash(f"{failure_sig}:{filtered}")
            continue
        if part.startswith("Spec update:") and "Judge added frontier" in part:
            compact = compact_discovery_context(ledger, part)
            if compact:
                out.append(compact)
                updates["posttool_last_discovery_headline"] = compact.split("\n", 1)[0].strip()
            continue
        if part.startswith("Spec update:"):
            filtered = filter_spec_update(ledger, part)
            if filtered:
                out.append(filtered)
                sig = _specupdate_signature(filtered)
                if sig:
                    updates["posttool_last_specupdate_sig"] = sig
            continue
        out.append(part)
    return out, updates


def emit_posttool_context(
    input_data: dict[str, Any],
    body: str,
    *,
    guidance_map: dict[str, dict[str, str]] | None = None,
    cache_updates: dict[str, str] | None = None,
) -> None:
    """Emit PostToolUse additionalContext once per unique body per turn epoch."""
    text = str(body or "").strip()
    if not text:
        emit_json({})
        return
    try:
        with _posttool_lock(input_data):
            ledger = load_ledger(input_data)
            epoch = block_epoch(input_data, ledger)
            digest = _body_hash(text)
            if ledger.get("posttool_context_epoch") == epoch and ledger.get("posttool_last_body_hash") == digest:
                emit_json({})
                return
            ledger["posttool_context_epoch"] = epoch
            ledger["posttool_last_body_hash"] = digest
            if guidance_map is not None:
                ledger["posttool_task_guidance"] = guidance_map
            for key, value in (cache_updates or {}).items():
                if value:
                    ledger[key] = value
            save_ledger(input_data, ledger)
    except Exception:
        pass
    emit_json(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            }
        }
    )
