#!/usr/bin/env python3
"""realtime_daemon frame routing + lifecycle logic (no real sockets).

Routing/assembly lives on a per-socket _Worker; idle-shutdown lives on the
JudgeDaemon that owns the worker pool. Exercises the router that multiplexes
out-of-band responses by response_id -> cid, assembles structured args + usage,
and fails open on errors, plus pool dispatch (least-busy worker, cid routing).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import realtime_daemon as jd  # noqa: E402


def _worker():
    return jd._Worker(0, threading.Event())


def _daemon(pool_size=1):
    return jd.JudgeDaemon("key", "/tmp/unifable-test-not-bound.sock", pool_size=pool_size)


def _register(w, cid, holder, rid=None):
    with w._state_lock:
        w._holders[cid] = holder
        if rid is not None:
            w._rid_to_cid[rid] = cid


def test_route_assembles_args_and_usage():
    w = _worker()
    h = jd._Holder()
    _register(w, 1, h)
    w._route({"type": "response.created", "response": {"id": "rid1", "metadata": {"cid": "1"}}})
    w._route({"type": "response.function_call_arguments.delta", "response_id": "rid1", "delta": '{"v":'})
    w._route({"type": "response.function_call_arguments.done", "response_id": "rid1", "arguments": '{"v":1}'})
    w._route(
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
    w = _worker()
    h = jd._Holder()
    _register(w, 3, h, rid="rid3")
    w._route(
        {
            "type": "response.done",
            "response": {"id": "rid3", "output": [{"type": "function_call", "arguments": '{"a":1}'}]},
        }
    )
    assert h.event.is_set()
    assert h.done_args == '{"a":1}'


def test_route_per_response_error_sets_holder_error():
    w = _worker()
    h = jd._Holder()
    _register(w, 2, h, rid="rid2")
    w._route({"type": "response.failed", "response": {"id": "rid2", "error": {"code": "bad", "message": "y"}}})
    assert h.event.is_set()
    assert h.error and "bad" in h.error


def test_route_session_error_fails_all_holders():
    w = _worker()
    h = jd._Holder()
    _register(w, 1, h)
    w._route({"type": "error", "error": {"code": "boom", "message": "x"}})
    assert h.event.is_set()
    assert h.error


def test_idle_shutdown_sets_stop():
    d = _daemon()
    d._workers = [_worker()]
    d._last_activity = time.monotonic() - (jd.IDLE_TTL + 10.0)
    d._maybe_idle_shutdown()
    assert d._stop.is_set()


def test_not_idle_when_inflight():
    d = _daemon()
    w = _worker()
    _register(w, 1, jd._Holder())
    d._workers = [w]
    d._last_activity = time.monotonic() - (jd.IDLE_TTL + 10.0)
    d._maybe_idle_shutdown()
    assert not d._stop.is_set()


def test_pick_worker_least_busy():
    d = _daemon(pool_size=3)
    d._workers = [_worker() for _ in range(3)]
    for i, w in enumerate(d._workers):
        w.idx = i
    # Load workers 0 and 1; worker 2 is idle -> should be picked.
    _register(d._workers[0], 10, jd._Holder())
    _register(d._workers[1], 11, jd._Holder())
    _register(d._workers[1], 12, jd._Holder())
    assert d._pick_worker().idx == 2


def test_pick_worker_ties_break_low_index():
    d = _daemon(pool_size=3)
    d._workers = [_worker() for _ in range(3)]
    for i, w in enumerate(d._workers):
        w.idx = i
    # All idle -> lowest index wins.
    assert d._pick_worker().idx == 0


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
