#!/usr/bin/env python3
"""judge_daemon frame routing + lifecycle logic (no real sockets).

Exercises the single-IO-thread router that multiplexes out-of-band responses by
response_id -> cid, assembles structured args + usage, and fails open on errors.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import judge_daemon as jd  # noqa: E402


def _daemon():
    return jd.JudgeDaemon("key", "/tmp/unifable-test-not-bound.sock")


def _register(d, cid, holder, rid=None):
    with d._state_lock:
        d._holders[cid] = holder
        if rid is not None:
            d._rid_to_cid[rid] = cid


def test_route_assembles_args_and_usage():
    d = _daemon()
    h = jd._Holder()
    _register(d, 1, h)
    d._route({"type": "response.created", "response": {"id": "rid1", "metadata": {"cid": "1"}}})
    d._route({"type": "response.function_call_arguments.delta", "response_id": "rid1", "delta": '{"v":'})
    d._route({"type": "response.function_call_arguments.done", "response_id": "rid1", "arguments": '{"v":1}'})
    d._route(
        {
            "type": "response.done",
            "response": {
                "id": "rid1",
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 3,
                    "total_tokens": 23,
                    "input_token_details": {"cached_tokens": 12},
                },
            },
        }
    )
    assert h.event.is_set()
    assert h.done_args == '{"v":1}'
    assert h.usage["cached_tokens"] == 12


def test_route_done_uses_function_args_fallback():
    d = _daemon()
    h = jd._Holder()
    _register(d, 3, h, rid="rid3")
    d._route(
        {
            "type": "response.done",
            "response": {"id": "rid3", "output": [{"type": "function_call", "arguments": '{"a":1}'}]},
        }
    )
    assert h.event.is_set()
    assert h.done_args == '{"a":1}'


def test_route_per_response_error_sets_holder_error():
    d = _daemon()
    h = jd._Holder()
    _register(d, 2, h, rid="rid2")
    d._route({"type": "response.failed", "response": {"id": "rid2", "error": {"code": "bad", "message": "y"}}})
    assert h.event.is_set()
    assert h.error and "bad" in h.error


def test_route_session_error_fails_all_holders():
    d = _daemon()
    h = jd._Holder()
    _register(d, 1, h)
    d._route({"type": "error", "error": {"code": "boom", "message": "x"}})
    assert h.event.is_set()
    assert h.error


def test_idle_shutdown_sets_stop():
    d = _daemon()
    d._last_activity = time.monotonic() - (jd.IDLE_TTL + 10.0)
    d._maybe_idle_shutdown()
    assert d._stop.is_set()


def test_not_idle_when_inflight():
    d = _daemon()
    _register(d, 1, jd._Holder())
    d._last_activity = time.monotonic() - (jd.IDLE_TTL + 10.0)
    d._maybe_idle_shutdown()
    assert not d._stop.is_set()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
