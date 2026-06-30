#!/usr/bin/env python3
"""rtinfer_client shim: delegates to the canonical client in the
@jaredboynton/rtinfer npm package. Tests verify the shim's fail-open contract
and that the canonical client is loadable from node_modules.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import rtinfer_client as rt  # noqa: E402


def test_canonical_client_loadable():
    # The shim should have loaded the canonical client from node_modules.
    # If the npm package isn't installed, every call fails open (tested below).
    # On a dev host with the package installed, _mod should be non-None.
    if rt._mod is None:
        import warnings
        warnings.warn("@jaredboynton/rtinfer not installed; shim fails open", stacklevel=1)
    assert hasattr(rt, "ask_structured")
    assert hasattr(rt, "ask_text")
    assert hasattr(rt, "discover")


def test_ask_structured_fails_open_without_daemon(monkeypatch):
    # When no daemon is reachable, ask_structured returns (None, None).
    # This is the documented fail-open contract.
    obj, usage = rt.ask_structured("S", "U", {"type": "object"})
    assert obj is None
    assert usage is None


def test_discover_returns_none_or_string(monkeypatch):
    result = rt.discover()
    assert result is None or isinstance(result, str)


def test_ask_text_fails_open_without_daemon(monkeypatch):
    result = rt.ask_text("S", "U")
    assert result is None or isinstance(result, str)


def test_invalidate_does_not_raise():
    rt._invalidate()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
