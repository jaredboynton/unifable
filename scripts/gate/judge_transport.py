#!/usr/bin/env python3
"""Daemon-aware judge transport seam (fail-open).

Drop-in replacement for ``codex_judge.ask_structured`` at hook call sites. When a
hook has bound the current session (``bind_session``) and the daemon is enabled,
judge requests route to the warm per-session WebSocket daemon for cache-stable,
handshake-free judging; on ANY daemon failure, unreachability, or timeout it falls
back to a direct ``codex_judge.ask_structured``. Token usage from either path is
recorded to the session ledger (``judge_usage.record_usage``) for cache measurement.

Outside hooks (CLI, tests, subagents) no session is bound, so this is exactly a
direct ``codex_judge.ask_structured`` call -- fully backward compatible.
"""

from __future__ import annotations

import contextvars
import os
from typing import Any

_SESSION: contextvars.ContextVar[dict | None] = contextvars.ContextVar("unifable_judge_session", default=None)


def bind_session(input_data: dict | None) -> None:
    """Bind the current hook's session so judge calls route to its daemon."""
    _SESSION.set(input_data if isinstance(input_data, dict) else None)


def _daemon_enabled() -> bool:
    return os.environ.get("UNIFABLE_JUDGE_DAEMON", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _accepts_on_usage(fn: Any) -> bool:
    """True if `fn` takes an `on_usage` kwarg (or **kwargs). Keeps the seam working
    when callers monkeypatch codex_judge.ask_structured with a narrower fake."""
    try:
        import inspect

        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True  # unintrospectable (e.g. builtins) -> assume the real signature
    if "on_usage" in params:
        return True
    return any(p.kind == p.VAR_KEYWORD for p in params.values())


def _record(input_data: dict | None, usage: dict[str, int] | None) -> None:
    if not isinstance(input_data, dict) or not isinstance(usage, dict) or not usage:
        return
    try:
        from judge_usage import record_usage
        from ledger import update_ledger

        update_ledger(input_data, lambda led: record_usage(led, usage))
    except Exception:
        pass


def ask_structured(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    schema_name: str = "result",
    **kwargs: Any,
) -> dict[str, Any]:
    """Judge one structured object, preferring the session daemon, else direct."""
    import codex_judge

    input_data = _SESSION.get()

    if input_data is None:
        # No bound session (CLI / tests / subagents): exactly a direct call, with
        # the original signature -- no usage sink, nothing to record against.
        return codex_judge.ask_structured(system, user, schema, schema_name=schema_name, **kwargs)

    if _daemon_enabled():
        try:
            from judge_client import daemon_ask

            obj, usage = daemon_ask(input_data, system, user, schema, schema_name=schema_name)
            if isinstance(obj, dict):
                _record(input_data, usage)
                return obj
        except Exception:
            pass  # fall through to a direct call

    if not _accepts_on_usage(codex_judge.ask_structured):
        return codex_judge.ask_structured(system, user, schema, schema_name=schema_name, **kwargs)

    def _sink(usage: dict[str, int]) -> None:
        _record(input_data, usage)

    return codex_judge.ask_structured(system, user, schema, schema_name=schema_name, on_usage=_sink, **kwargs)
