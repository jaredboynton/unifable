#!/usr/bin/env python3
"""PreToolUse block message compression and turn-scoped deduplication.

Codex (and other hosts) may invoke PreToolUse hooks concurrently for parallel tool
calls. Without coordination each blocked call prints the full stderr message.

Change-only stderr policy:
- First block per (epoch, block_signature) emits full or compact instructions.
- Identical retries (same kind+detail) emit nothing — exit 2 only.
- A new signature in the same turn emits compact output (cite lines kept, unlock
  footer not repeated).
- Gate cleared (additionalContext on allow) signals a state transition.

Exit code 2 is the block signal; stderr is elaboration on first sighting or when
the reason changes, not on every retry.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from ledger import ledger_path, load_ledger, save_ledger
    from research_bash_guidance import bash_allowed_summary
except ImportError:  # pragma: no cover
    from scripts.gate.ledger import ledger_path, load_ledger, save_ledger
    from scripts.gate.research_bash_guidance import bash_allowed_summary

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

GATE_PREFIX = ""

_WHITELIST_DETAIL_RE = re.compile(r"^(\S+) is not in the Bash research whitelist$", re.IGNORECASE)
_PIPELINE_DETAIL_RE = re.compile(r"^(\S+) is not an allowed read-only pipeline sink$", re.IGNORECASE)

_RESTATE_LINE = "1. unifable restate '<goal in your own words>'"
_ADD_TASK_LINE = "2. unifable add-task --title '<requirement>' --check '<runnable check>'"
_HEAVY_SET_PRIMARY_LINE = "3. unifable set-primary --title '<fallback approach>' --check '<runnable proof>'"
_HEAVY_ADD_FRONTIER_LINE = "4. unifable add-frontier --title '<approach>' --check '<exploration check>' twice, for two distinct approaches"
_ALLOWED_NOW_PREFIX = "Allowed now:"


@dataclass(frozen=True)
class BlockContext:
    scaffold_notified: bool = False
    unlock_footer_sent: bool = False
    allowlist_sent: bool = False
    contract_notified: bool = False


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


def block_context(input_data: dict[str, Any], ledger: dict[str, Any] | None = None) -> BlockContext:
    """Ledger-derived flags for action-only block formatting."""
    try:
        data = ledger if isinstance(ledger, dict) else load_ledger(input_data)
        epoch = block_epoch(input_data, data)
        return BlockContext(
            scaffold_notified=bool(data.get("prompt_scaffold_notified")),
            unlock_footer_sent=data.get("pretool_unlock_footer_epoch") == epoch,
            allowlist_sent=data.get("pretool_allowlist_notified_epoch") == epoch,
            contract_notified=bool(data.get("prompt_scaffold_notified"))
            or data.get("spec_contract_notified_epoch") == epoch,
        )
    except Exception:
        return BlockContext()


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


def pretool_headline_only(message: str) -> str:
    """First line of a block message (drop shared unlock footer)."""
    text = str(message or "").strip()
    if not text:
        return ""
    return text.split("\n", 1)[0].strip()


def _join_lines(lines: list[str]) -> str:
    return "\n".join(line for line in lines if line.strip())


def _append_unlock(lines: list[str], ctx: BlockContext) -> None:
    if not ctx.scaffold_notified and not ctx.unlock_footer_sent:
        lines.extend(("Next:", _RESTATE_LINE, _ADD_TASK_LINE))


def _append_heavy_unlock(lines: list[str], ctx: BlockContext) -> None:
    if not ctx.scaffold_notified and not ctx.unlock_footer_sent:
        lines.extend((_HEAVY_SET_PRIMARY_LINE, _HEAVY_ADD_FRONTIER_LINE))


def _append_bash_allowlist(lines: list[str], ctx: BlockContext) -> None:
    if not ctx.allowlist_sent:
        lines.append(f"{_ALLOWED_NOW_PREFIX} {bash_allowed_summary()}.")


def message_includes_unlock(message: str) -> bool:
    text = str(message or "")
    return (
        _RESTATE_LINE in text
        or _ADD_TASK_LINE in text
        or _HEAVY_SET_PRIMARY_LINE in text
        or _HEAVY_ADD_FRONTIER_LINE in text
        or text.strip().startswith("Unlock:")
    )


def message_includes_allowlist(message: str) -> bool:
    return _ALLOWED_NOW_PREFIX in str(message or "")


def is_boilerplate_only(message: str) -> bool:
    """True when the block is only unlock/allowlist boilerplate with no novel why."""
    text = str(message or "").strip()
    if not text:
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        if line == "Next:":
            continue
        if line in {_RESTATE_LINE, _ADD_TASK_LINE, _HEAVY_SET_PRIMARY_LINE, _HEAVY_ADD_FRONTIER_LINE}:
            continue
        if line.startswith(_ALLOWED_NOW_PREFIX):
            continue
        if line.startswith("Allowed now: Read/Grep/Glob/web"):
            continue
        return False
    return bool(lines)


def is_redundant_with_notify(message: str, notify: str) -> bool:
    """True when notify already carries the block's actionable content."""
    msg = str(message or "").strip()
    note = str(notify or "").strip()
    if not msg or not note:
        return False
    if msg in note or note in msg:
        return True
    headline = pretool_headline_only(msg)
    if headline and headline in note:
        return True
    return is_boilerplate_only(msg)


def compact_pretool_output(message: str, *, footer_sent: bool) -> str:
    """Shrink a block when the unlock footer already went out this turn.

    Drops repeated unlock/allowlist boilerplate; keeps the headline and any
    indented detail lines (e.g. per-cite list)."""
    text = str(message or "").strip()
    if not text or not footer_sent:
        return text
    actionable: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "Next:":
            continue
        if stripped in {_RESTATE_LINE, _ADD_TASK_LINE, _HEAVY_SET_PRIMARY_LINE, _HEAVY_ADD_FRONTIER_LINE}:
            continue
        if stripped.startswith(_ALLOWED_NOW_PREFIX) or stripped.startswith("Allowed now: Read/Grep"):
            continue
        actionable.append(line.rstrip())
    if not actionable:
        return "(see the earlier instruction this turn.)"
    headline = actionable[0].strip()
    detail_lines = [ln for ln in actionable[1:] if ln.startswith("  ")]
    if detail_lines:
        return headline + "\n" + "\n".join(detail_lines)
    return headline


def format_bash_research_block(
    why: str,
    session_id: str = "",
    *,
    ctx: BlockContext | None = None,
) -> str:
    """Action-only block for bash research-phase whitelist failures."""
    _ = session_id
    why = " ".join(str(why or "").split())
    ctx = ctx or BlockContext()
    lines: list[str] = []
    if why:
        lines.append(f"{why}.")
    _append_unlock(lines, ctx)
    if not ctx.allowlist_sent:
        lines.append(f"{_ALLOWED_NOW_PREFIX} inspection tools: Read, Grep, Glob, WebSearch, WebFetch, NotebookRead.")
        lines.append(f"Bash allowlist: {bash_allowed_summary()}.")
    return _join_lines(lines)


def format_bash_policy_block(
    why: str,
    session_id: str = "",
    *,
    ctx: BlockContext | None = None,
) -> str:
    """Action-only block for Bash commands disallowed even after unlock."""
    _ = session_id
    _ = ctx
    why = " ".join(str(why or "").split())
    return f"{why}." if why else ""


def format_delegation_block(
    tool_name: str,
    session_id: str = "",
    *,
    ctx: BlockContext | None = None,
) -> str:
    """Action-only block for Task/Agent delegation lockdown."""
    _ = tool_name
    _ = session_id
    ctx = ctx or BlockContext()
    lines: list[str] = []
    _append_unlock(lines, ctx)
    _append_heavy_unlock(lines, ctx)
    if not ctx.allowlist_sent:
        lines.append(
            "Allowed now: inspection tools (Read, Grep, Glob, WebSearch, WebFetch, NotebookRead)."
        )
        lines.append(f"Bash allowlist: {bash_allowed_summary()}.")
    return _join_lines(lines)


def format_spec_missing_block(
    grade: str,
    session_id: str,
    contract: str,
    *,
    ctx: BlockContext | None = None,
) -> str:
    """Action-only block when no evidence spec exists yet."""
    _ = session_id
    _ = contract
    ctx = ctx or BlockContext()
    if ctx.scaffold_notified:
        return ""
    grade = (grade or "STANDARD").upper()
    if not ctx.unlock_footer_sent:
        lines = [f"Evidence spec required (grade={grade})."]
        _append_unlock(lines, ctx)
        if grade == "HEAVY":
            _append_heavy_unlock(lines, ctx)
        return _join_lines(lines)
    return f"Evidence spec required (grade={grade})."


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


def _record_pretool_block_in_lock(
    input_data: dict[str, Any],
    ledger: dict[str, Any],
    kind: str,
    detail: str,
) -> None:
    ledger["pretool_last_block_kind"] = str(kind or "")[:40]
    ledger["pretool_last_block_detail"] = " ".join(str(detail or "").split())[:200]


def _mark_footer_flags(input_data: dict[str, Any], message: str) -> None:
    """Persist unlock/allowlist dedup epochs when those lines were emitted."""
    if not message.strip():
        return
    try:
        with _pretool_lock(input_data):
            ledger = load_ledger(input_data)
            epoch = block_epoch(input_data, ledger)
            if message_includes_unlock(message):
                ledger["pretool_unlock_footer_epoch"] = epoch
            if message_includes_allowlist(message):
                ledger["pretool_allowlist_notified_epoch"] = epoch
            save_ledger(input_data, ledger)
    except Exception:
        pass


def consume_gate_cleared_notify(
    input_data: dict[str, Any],
    hygiene_headlines: list[str] | None = None,
) -> str:
    """Build Gate cleared additionalContext when a prior block was recorded."""
    try:
        from ledger import load_ledger, update_ledger

        ledger = load_ledger(input_data)
        if not ledger.get("pretool_last_block_kind"):
            lines = [str(h).strip() for h in (hygiene_headlines or []) if str(h).strip()]
            return "\n".join(lines)

        def clear(ld: dict[str, Any]) -> None:
            ld.pop("pretool_last_block_kind", None)
            ld.pop("pretool_last_block_detail", None)

        update_ledger(input_data, clear)
        lines: list[str] = []
        for h in hygiene_headlines or []:
            text = str(h).strip()
            if text and text not in lines:
                lines.append(text)
        return "\n".join(lines)
    except Exception:
        return ""


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
        with _pretool_lock(input_data):
            ledger = load_ledger(input_data)
            epoch = block_epoch(input_data, ledger)
            footer_sent = ledger.get("pretool_unlock_footer_epoch") == epoch
            if ledger.get("pretool_block_epoch") != epoch:
                ledger["pretool_block_epoch"] = epoch
                ledger["pretool_block_counts"] = {}
                ledger["pretool_unlock_footer_epoch"] = ""
                ledger["pretool_allowlist_notified_epoch"] = ""
                footer_sent = False
            counts = ledger.get("pretool_block_counts")
            if not isinstance(counts, dict):
                counts = {}
            prev = int(counts.get(sig, 0))
            count = prev + 1
            counts[sig] = count
            ledger["pretool_block_counts"] = counts
            _record_pretool_block_in_lock(input_data, ledger, kind, detail)
            save_ledger(input_data, ledger)
        if message and count == 1:
            out = compact_pretool_output(message, footer_sent=footer_sent)
            if out:
                print(f"{GATE_PREFIX}{out}", file=sys.stderr)
                _mark_footer_flags(input_data, out)
        elif message and not str(input_data.get("turn_id") or "").strip():
            headline = pretool_headline_only(message)
            if headline:
                print(
                    f"{GATE_PREFIX}{headline} "
                    "(repeat -- same block as the earlier instruction this session; "
                    "see it for the next step.)",
                    file=sys.stderr,
                )
        return 2
    except Exception:
        if message:
            print(f"{GATE_PREFIX}{message}", file=sys.stderr)
        return 2
