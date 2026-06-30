#!/usr/bin/env python3
"""Shared rtinfer/1 HTTP client -- discover and use the always-on realtime daemon.

The cse-tools daemon (`cse-toold`) exposes a loopback `/v1/infer` endpoint serving
warm realtime + responses model pools. When that daemon is present on this
machine, unifable's judge can borrow it instead of spawning its own per-session
WebSocket daemon: same models, one warm pool, no second auth path. Neither repo
imports the other; discovery is purely by loopback URL + a shared well-known file.

This is a *preferred* path, never a required one. It fails open exactly like the
rest of the gate: any unreachability, timeout, or non-OK envelope returns
``(None, None)`` so ``judge_transport`` falls through to the existing per-session
daemon and then a direct ``codex_judge.ask_structured``. If unifable is installed
on a host with no cse-toold, nothing here ever fires.

Discovery order (matches the cse-sweep client, scripts/lib/daemon-client.mjs):
  1. $CSE_RTINFER_URL              explicit override / tests
  2. http://127.0.0.1:8787         cse-toold cockpit default
  3. ~/.cse-rtinfer/endpoint.json  {contract:"rtinfer/1", base_url:...}

Stdlib only: urllib + json.

# cleanup-traps: not-applicable -- stateless HTTP client, no spawned process
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONTRACT = "rtinfer/1"
_COCKPIT_DEFAULT = "http://127.0.0.1:8787"
_WELL_KNOWN = Path.home() / ".cse-rtinfer" / "endpoint.json"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


HEALTH_TIMEOUT = _env_float("CSE_RTINFER_HEALTH_TIMEOUT", 0.5)
REQUEST_TIMEOUT = _env_float("CSE_RTINFER_REQUEST_TIMEOUT", 95.0)
# Re-discovery is cheap but not free; cache the resolved base for this process.
_DISCOVERY_TTL = _env_float("CSE_RTINFER_DISCOVERY_TTL", 30.0)

_resolved_at = 0.0
_resolved_base: str | None = None


def enabled() -> bool:
    """Opt-in borrow of the shared cse-tools daemon. Default OFF so the mature
    per-session judge path stays byte-identical and every protected test is
    deterministic regardless of whether a cse-toold happens to be running on the
    host. Set ``UNIFABLE_JUDGE_RTINFER=1`` to prefer the shared daemon."""
    return os.environ.get("UNIFABLE_JUDGE_RTINFER", "0").strip().lower() in ("1", "true", "yes", "on")


def _candidates() -> list[str]:
    out: list[str] = []
    override = os.environ.get("CSE_RTINFER_URL")
    if override:
        out.append(override.strip())
    out.append(_COCKPIT_DEFAULT)
    try:
        data = json.loads(_WELL_KNOWN.read_text("utf-8"))
        if isinstance(data, dict) and data.get("contract") == CONTRACT and data.get("base_url"):
            out.append(str(data["base_url"]).strip())
    except (OSError, ValueError):
        pass
    return out


def _health_ok(base: str) -> bool:
    url = base.rstrip("/") + "/v1/infer/health"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as resp:  # noqa: S310 (loopback only)
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False
    return isinstance(data, dict) and data.get("contract") == CONTRACT and data.get("ready") is True


def discover(refresh: bool = False) -> str | None:
    """Resolve a ready rtinfer base URL, or None. Cached for _DISCOVERY_TTL."""
    global _resolved_at, _resolved_base
    if not enabled():
        return None
    now = time.monotonic()
    if not refresh and _resolved_base is not None and (now - _resolved_at) < _DISCOVERY_TTL:
        return _resolved_base
    for base in _candidates():
        if _health_ok(base):
            _resolved_base = base.rstrip("/")
            _resolved_at = now
            return _resolved_base
    _resolved_base = None
    _resolved_at = now
    return None


def ask_structured(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    schema_name: str = "result",
    model: str | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[dict[str, Any] | None, dict[str, int] | None]:
    """One structured ask over the shared daemon's realtime tier. Returns
    ``(object, usage)`` on success, ``(None, None)`` to signal fallback.

    ``usage`` is always None: the loopback endpoint does not surface token
    counts, and the borrow path is off the correctness/measurement path."""
    base = discover()
    if base is None:
        return None, None
    body = {
        "contract": CONTRACT,
        "tier": "realtime_structured",
        "system": system,
        "user": user,
        "schema": schema,
        "schema_name": schema_name,
    }
    if model:
        body["model"] = model
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + "/v1/infer",
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (loopback only)
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        # Daemon went away mid-run: invalidate so the next call re-discovers.
        _invalidate()
        return None, None
    if not isinstance(data, dict) or data.get("ok") is not True:
        return None, None
    obj = data.get("object")
    if not isinstance(obj, dict):
        return None, None
    return obj, None


def _invalidate() -> None:
    global _resolved_at, _resolved_base
    _resolved_base = None
    _resolved_at = 0.0
