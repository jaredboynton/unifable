#!/usr/bin/env python3
"""PostToolUse additionalContext dedup and ledger-backed guidance cache."""

from __future__ import annotations

import contextlib
import hashlib
import os
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
        if part.startswith("unifable spec update:") and "Judge added frontier" in part:
            compact = compact_discovery_context(ledger, part)
            if compact:
                out.append(compact)
                updates["posttool_last_discovery_headline"] = compact.split("\n", 1)[0].strip()
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
            first_line = text.split("\n", 1)[0]
            if first_line.startswith("synced ") and " cite(s):" in first_line:
                ledger["posttool_last_cite_headline"] = first_line
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


def should_suppress_cite_only(
    spec: dict[str, Any],
    ledger: dict[str, Any],
    cite_headline: str,
) -> bool:
    """Skip cite-only noise when the model already has guidance for open tasks."""
    headline = str(cite_headline or "").strip()
    if not headline.startswith("synced ") or " cite(s):" not in headline:
        return False
    try:
        from model_notify import guidance_covers_incomplete
    except ImportError:
        from scripts.gate.model_notify import guidance_covers_incomplete  # pragma: no cover
    if not guidance_covers_incomplete(spec, ledger):
        return False
    last = str(ledger.get("posttool_last_cite_headline") or "").strip()
    return last == headline
