#!/usr/bin/env python3
"""Judge token-usage accounting (host-agnostic, no I/O).

Parses gpt-realtime-2 ``response.done`` / ``response.completed`` usage envelopes
and accumulates per-session counters into a ledger dict. Callers persist the
ledger. Cache-hit rates measured here are what justify the caching
rearchitecture (prefix stabilization + transcript caching + warm-socket daemon):
without ``cached_tokens`` there is no way to know whether any of it works.
"""

from __future__ import annotations

from typing import Any


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_usage(env: dict[str, Any]) -> dict[str, int] | None:
    """Extract token usage from a response.done/response.completed envelope.

    Returns None when no usage block is present (e.g. delta frames). Looks under
    ``response.usage`` first (Realtime shape) then a top-level ``usage``.
    """
    if not isinstance(env, dict):
        return None
    usage: Any = None
    resp = env.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("usage"), dict):
        usage = resp["usage"]
    elif isinstance(env.get("usage"), dict):
        usage = env["usage"]
    if not isinstance(usage, dict):
        return None
    cached = 0
    details = usage.get("input_token_details")
    if isinstance(details, dict):
        cached = _int(details.get("cached_tokens"))
    return {
        "input_tokens": _int(usage.get("input_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "cached_tokens": cached,
        "total_tokens": _int(usage.get("total_tokens")),
    }


def record_usage(ledger: dict[str, Any], usage: dict[str, int] | None) -> None:
    """Accumulate one parsed usage record into ledger counters (mutates ledger)."""
    if not isinstance(ledger, dict) or not isinstance(usage, dict):
        return

    def _acc(key: str, add: Any) -> None:
        ledger[key] = _int(ledger.get(key)) + _int(add)

    _acc("judge_calls", 1)
    _acc("judge_input_tokens", usage.get("input_tokens"))
    _acc("judge_cached_tokens", usage.get("cached_tokens"))
    _acc("judge_output_tokens", usage.get("output_tokens"))
    ledger["judge_last_usage"] = {
        "input_tokens": _int(usage.get("input_tokens")),
        "cached_tokens": _int(usage.get("cached_tokens")),
        "output_tokens": _int(usage.get("output_tokens")),
        "total_tokens": _int(usage.get("total_tokens")),
    }
