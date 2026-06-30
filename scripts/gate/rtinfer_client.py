#!/usr/bin/env python3
"""Thin re-export shim: loads the canonical rtinfer/1 Python client shipped in
the ``@jaredboynton/rtinfer`` npm package (installed at
``node_modules/@jaredboynton/rtinfer/clients/rtinfer_client.py``).

The canonical client is the source of truth for discovery, health gating, and
the wire contract. This shim exists so ``judge_transport.py`` and tests can keep
using ``from rtinfer_client import ask_structured`` with ``scripts/gate`` on
``sys.path`` while the actual implementation lives in the npm package.

If the npm package is not installed, every function fails open: ``ask_structured``
returns ``(None, None)``, ``discover`` returns ``None``. This is the documented
fail-open contract: no daemon, no inference, fall through to the next path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_CANONICAL = _HERE.parent.parent / "node_modules" / "@jaredboynton" / "rtinfer" / "clients" / "rtinfer_client.py"

_mod = None
if _CANONICAL.is_file():
    _spec = importlib.util.spec_from_file_location("_rtinfer_canonical", _CANONICAL)
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules["_rtinfer_canonical"] = _mod
        try:
            _spec.loader.exec_module(_mod)
        except Exception:
            _mod = None


def ask_structured(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    schema_name: str = "result",
    model: str | None = None,
    timeout: float | None = None,
) -> tuple[dict[str, Any] | None, dict[str, int] | None]:
    if _mod is None:
        return None, None
    kwargs: dict[str, Any] = {"schema_name": schema_name, "model": model}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return _mod.ask_structured(system, user, schema, **kwargs)


def ask_text(
    system: str,
    user: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
) -> str | None:
    if _mod is None:
        return None
    kwargs: dict[str, Any] = {"model": model}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return _mod.ask_text(system, user, **kwargs)


def discover(refresh: bool = False) -> str | None:
    if _mod is None:
        return None
    return _mod.discover(refresh)


def _invalidate() -> None:
    if _mod is not None:
        _mod._invalidate()
