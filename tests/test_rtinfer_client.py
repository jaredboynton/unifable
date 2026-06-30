#!/usr/bin/env python3
"""rtinfer_client: discovery + fail-open contract for borrowing the shared
cse-tools rtinfer daemon. Opt-in (UNIFABLE_JUDGE_RTINFER); off by default so the
mature per-session judge path is untouched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import rtinfer_client as rt  # noqa: E402


def _reset():
    rt._invalidate()


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("UNIFABLE_JUDGE_RTINFER", raising=False)
    _reset()
    assert rt.enabled() is False
    # discover() short-circuits to None when disabled, regardless of any daemon.
    assert rt.discover() is None


def test_ask_returns_fallback_signal_when_unreachable(monkeypatch):
    monkeypatch.setenv("UNIFABLE_JUDGE_RTINFER", "1")
    monkeypatch.setattr(rt, "_candidates", lambda: ["http://127.0.0.1:1"])
    _reset()
    obj, usage = rt.ask_structured("S", "U", {"type": "object"})
    assert obj is None and usage is None


def test_ask_parses_ok_envelope(monkeypatch):
    monkeypatch.setenv("UNIFABLE_JUDGE_RTINFER", "1")
    monkeypatch.setattr(rt, "discover", lambda refresh=False: "http://127.0.0.1:8787")

    class FakeResp:
        status = 200

        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        return FakeResp({"contract": "rtinfer/1", "ok": True, "tier": "realtime_structured", "object": {"verdict": 1}})

    monkeypatch.setattr(rt.urllib.request, "urlopen", fake_urlopen)
    obj, usage = rt.ask_structured("S", "U", {"type": "object"}, schema_name="x")
    assert obj == {"verdict": 1}
    assert usage is None


def test_ask_rejects_non_ok_envelope(monkeypatch):
    monkeypatch.setenv("UNIFABLE_JUDGE_RTINFER", "1")
    monkeypatch.setattr(rt, "discover", lambda refresh=False: "http://127.0.0.1:8787")

    class FakeResp:
        status = 200

        def read(self):
            return json.dumps({"contract": "rtinfer/1", "ok": False, "error": {"code": "x"}}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rt.urllib.request, "urlopen", lambda req, timeout=None: FakeResp())
    obj, usage = rt.ask_structured("S", "U", {"type": "object"})
    assert obj is None and usage is None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
