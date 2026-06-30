#!/usr/bin/env python3
"""Shared rtinfer/1 HTTP client -- discover and use the always-on rtinferd daemon.

The standalone rtinferd daemon (repo: rtinfer) exposes a loopback `/v1/infer`
endpoint serving warm realtime + responses model pools. It advertises itself via
``~/.cse-rtinfer/endpoint.json``. When rtinferd is present on this machine,
unifable's judge can borrow it instead of spawning its own per-session WebSocket
daemon: same models, one warm pool, no second auth path.

This is a *preferred* path, never a required one. It fails open exactly like the
rest of the gate: any unreachability, timeout, or non-OK envelope returns
``(None, None)`` so ``judge_transport`` falls through to the existing per-session
daemon and then a direct ``codex_judge.ask_structured``.

Discovery order:
  1. $CSE_RTINFER_URL              explicit override / tests
  2. ~/.cse-rtinfer/endpoint.json  {contract:"rtinfer/1", base_url:...}

LOCKSTEP CONTRACT: the rtinfer/1 wire shape lives in THREE clients that must be
edited together when the contract bumps:
  - this file (unifable judge path)
  - skills/unitrace/scripts/lib/rtinfer-client.mjs (unifable search/daemon path)
  - rtinfer clients/js/rtinfer-client.mjs + clients/python/rtinfer_client.py
The health gate accepts any rtinfer/1.x (major-1 match), so a minor bump does not
dark-fail; a true rtinfer/2 cleanly falls open.

TIMEOUTS: the judge uses 95s (CSE_RTINFER_REQUEST_TIMEOUT) for long structured
synthesis. The search mirror (rtinfer-client.mjs) deliberately uses a tighter 20s
via UNITRACE_SEARCH_RTINFER_REQUEST_TIMEOUT. The defaults differ ON PURPOSE.

Stdlib only: urllib + json.

# cleanup-traps: not-applicable -- stateless HTTP client, no spawned process
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONTRACT = "rtinfer/1"
_CONTRACT_MAJOR = 1
_WELL_KNOWN = Path.home() / ".cse-rtinfer" / "endpoint.json"


def _contract_major_ok(contract: Any) -> bool:
    """True when ``contract`` is rtinfer/<major>.* matching _CONTRACT_MAJOR."""
    if not isinstance(contract, str):
        return False
    m = re.match(r"^rtinfer/(\d+)", contract)
    return bool(m) and int(m.group(1)) == _CONTRACT_MAJOR


def _debug_log(msg: str) -> None:
    if (os.environ.get("UNIFABLE_DEBUG") or os.environ.get("DEBUG") or "").strip():
        try:
            sys.stderr.write(f"[rtinfer] {msg}\n")
        except OSError:
            pass


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


def _env_bool(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _candidates() -> list[str]:
    out: list[str] = []
    override = os.environ.get("CSE_RTINFER_URL")
    if override:
        out.append(override.strip())
    # Strict mode: trust ONLY the explicit override, no well-known fallback
    # (mirrors rtinfer-client.mjs CSE_RTINFER_STRICT_URL). Default off keeps
    # the documented discovery order.
    if override and _env_bool("CSE_RTINFER_STRICT_URL"):
        return out
    try:
        data = json.loads(_WELL_KNOWN.read_text("utf-8"))
        if isinstance(data, dict) and _contract_major_ok(data.get("contract")) and data.get("base_url"):
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
    if not isinstance(data, dict):
        return False
    if not _contract_major_ok(data.get("contract")):
        if data.get("contract"):
            _debug_log(f"contract mismatch at {base}: {data.get('contract')} (want rtinfer/{_CONTRACT_MAJOR}.x)")
        return False
    return data.get("ready") is True


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
