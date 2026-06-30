#!/usr/bin/env python3
"""judge_transport fail-open seam: with no session bound or the daemon disabled,
it is exactly a direct codex_judge.ask_structured call, and token usage from the
direct path is still recorded to the session ledger.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import codex_judge  # noqa: E402
import judge_transport as jt  # noqa: E402


def test_no_session_is_direct(monkeypatch):
    seen = {}

    def fake(system, user, schema, *, schema_name="result", on_usage=None, **kw):
        seen["system"] = system
        return {"verdict": 1}

    monkeypatch.setattr(codex_judge, "ask_structured", fake)
    jt.bind_session(None)
    try:
        out = jt.ask_structured("S", "U", {"type": "object"}, schema_name="x")
    finally:
        jt.bind_session(None)
    assert out == {"verdict": 1}
    assert seen["system"] == "S"


def test_daemon_disabled_falls_back_and_records_usage(monkeypatch):
    monkeypatch.setenv("UNIFABLE_JUDGE_DAEMON", "0")
    recorded: dict = {}

    def fake_update(input_data, fn):
        led: dict = {}
        fn(led)
        recorded.update(led)

    monkeypatch.setattr("ledger.update_ledger", fake_update)

    def fake(system, user, schema, *, schema_name="result", on_usage=None, **kw):
        if on_usage:
            on_usage({"input_tokens": 100, "cached_tokens": 40, "output_tokens": 5, "total_tokens": 105})
        return {"ok": 1}

    monkeypatch.setattr(codex_judge, "ask_structured", fake)
    jt.bind_session({"session_id": "s", "cwd": "/tmp"})
    try:
        out = jt.ask_structured("S", "U", {"type": "object"})
    finally:
        jt.bind_session(None)
    assert out == {"ok": 1}
    assert recorded.get("judge_cached_tokens") == 40
    assert recorded.get("judge_calls") == 1


def test_daemon_failure_falls_back(monkeypatch):
    monkeypatch.setenv("UNIFABLE_JUDGE_DAEMON", "1")
    monkeypatch.setattr("ledger.update_ledger", lambda input_data, fn: fn({}))

    def boom(*a, **k):
        raise RuntimeError("rtinfer exploded")

    monkeypatch.setattr("rtinfer_client.ask_structured", boom)

    def fake(system, user, schema, *, schema_name="result", on_usage=None, **kw):
        return {"fallback": True}

    monkeypatch.setattr(codex_judge, "ask_structured", fake)
    jt.bind_session({"session_id": "s", "cwd": "/tmp"})
    try:
        out = jt.ask_structured("S", "U", {"type": "object"})
    finally:
        jt.bind_session(None)
    assert out == {"fallback": True}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
