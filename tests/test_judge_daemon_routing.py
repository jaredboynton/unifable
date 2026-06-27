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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import realtime_daemon as jd  # noqa: E402
import judge_client as jc  # noqa: E402


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


def test_worker_load_counts_inflight_plus_queued():
    w = _worker()
    _register(w, 1, jd._Holder())
    _register(w, 2, jd._Holder())
    for cid in (10, 11, 12):
        w.enqueue(cid, {}, jd._Holder())
    assert w.inflight() == 2
    assert w.load() == 5


def test_pick_worker_spreads_first_burst_across_pool():
    d = _daemon(pool_size=4)
    d._workers = [_worker() for _ in range(4)]
    for i, w in enumerate(d._workers):
        w.idx = i
    chosen: list[int] = []
    for cid in range(4):
        worker = d._pick_worker()
        chosen.append(worker.idx)
        worker.enqueue(cid, {}, jd._Holder())
    assert chosen == [0, 1, 2, 3]


def test_serial_single_pick_still_worker_zero():
    d = _daemon(pool_size=4)
    d._workers = [_worker() for _ in range(4)]
    for i, w in enumerate(d._workers):
        w.idx = i
    assert d._pick_worker().idx == 0


def test_submit_overload_counts_queued_load(monkeypatch):
    monkeypatch.setattr(jd, "MAX_INFLIGHT", 1)
    d = _daemon(pool_size=1)
    w = _worker()
    w.idx = 0
    d._workers = [w]
    w.enqueue(1, {}, jd._Holder())
    assert w.load() == 1
    with pytest.raises(RuntimeError, match="overloaded"):
        d.submit({"system": "", "user": "", "schema": {}}, timeout=0.01)


def test_sock_path_default_model_is_legacy_unsuffixed():
    # gpt-realtime-2 (DEFAULT_MODEL) MUST keep the original un-suffixed socket so
    # the judge daemon process and prompt-cache stickiness are unchanged.
    path = jc._sock_path("abc123")
    assert path.name == "abc123.sock"
    assert jc._sock_path("abc123", jc.DEFAULT_MODEL).name == "abc123.sock"


def test_sock_path_other_model_is_namespaced():
    # A non-default model gets a stable per-model hash suffix so it never shares a
    # socket/process with the gpt-realtime-2 judge.
    mini = jc._sock_path("abc123", "gpt-realtime-mini")
    assert mini.name != "abc123.sock"
    assert mini.name.startswith("abc123-")
    assert mini.name.endswith(".sock")
    # Deterministic + model-distinct.
    assert mini == jc._sock_path("abc123", "gpt-realtime-mini")
    assert mini != jc._sock_path("abc123", "some-other-model")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
