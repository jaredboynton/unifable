#!/usr/bin/env python3
"""Host-aware hook stdout shaping (Codex vs Claude Code)."""

from __future__ import annotations

import os
from typing import Any, Literal

HostKind = Literal["codex", "claude", "unknown"]

_STOP_REASON_MAX = 32_000


def detect_host(input_data: dict[str, Any] | None = None) -> HostKind:
    """Best-effort host detection for hook output contracts."""
    forced = os.environ.get("UNIFABLE_HOST", "").strip().lower()
    if forced in ("codex", "claude"):
        return forced  # type: ignore[return-value]

    data = input_data if isinstance(input_data, dict) else {}
    if data.get("turn_id"):
        return "codex"

    for var in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "UNIFABLE_PLUGIN_ROOT"):
        raw = os.environ.get(var, "").strip()
        if "/.codex/" in raw:
            return "codex"
        if "/.claude/" in raw:
            return "claude"

    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    return "unknown"


def _merge_reason_parts(*parts: str) -> str:
    return "\n\n".join(p.strip() for p in parts if p and str(p).strip())


def _truncate_reason(reason: str, *, digest_path: str = "") -> str:
    text = (reason or "").strip()
    if len(text) <= _STOP_REASON_MAX:
        return text
    note = f"\n\n(Full digest truncated; see {digest_path})" if digest_path else "\n\n(Full digest truncated.)"
    budget = max(0, _STOP_REASON_MAX - len(note))
    return text[:budget].rstrip() + note


def attach_stop_validate_context(payload: dict[str, Any], ctx: str) -> None:
    """Claude Code Stop: spec digest via hookSpecificOutput.additionalContext."""
    if not ctx or not ctx.strip():
        return
    hso = payload.setdefault("hookSpecificOutput", {})
    hso["hookEventName"] = "Stop"
    existing = str(hso.get("additionalContext") or "").strip()
    hso["additionalContext"] = f"{existing}\n{ctx}".strip() if existing else ctx


def finalize_stop_payload(
    payload: dict[str, Any],
    *,
    validate_ctx: str = "",
    loop_lift_ctx: str = "",
    input_data: dict[str, Any] | None = None,
    digest_path: str = "",
    host: HostKind | None = None,
) -> dict[str, Any]:
    """Return *payload* shaped for the active host Stop hook contract."""
    kind = host or detect_host(input_data)
    extra = _merge_reason_parts(validate_ctx, loop_lift_ctx)

    if kind == "codex":
        payload.pop("hookSpecificOutput", None)
        if extra and payload.get("decision") == "block":
            base = str(payload.get("reason") or "").strip()
            payload["reason"] = _truncate_reason(
                _merge_reason_parts(base, extra),
                digest_path=digest_path,
            )
        return payload

    if extra:
        attach_stop_validate_context(payload, extra)
    return payload
