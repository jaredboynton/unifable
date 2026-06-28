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

import judge_client as jc  # noqa: E402
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
    # Load workers 0 and 1; worker 2 is idle -> a COLD family (no sticky home yet,
    # and the test workers are not ready() so sticky never engages) picks least-busy.
    _register(d._workers[0], 10, jd._Holder())
    _register(d._workers[1], 11, jd._Holder())
    _register(d._workers[1], 12, jd._Holder())
    assert d._pick_worker(hash("cold")).idx == 2


def test_pick_worker_ties_break_low_index():
    d = _daemon(pool_size=3)
    d._workers = [_worker() for _ in range(3)]
    for i, w in enumerate(d._workers):
        w.idx = i
    # All idle, cold family -> least-busy tie breaks to lowest index.
    assert d._pick_worker(hash("cold")).idx == 0


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
    # Distinct cold families (and not-ready workers -> sticky never engages), so
    # each pick is least-busy and the burst spreads across the whole pool.
    for cid in range(4):
        worker = d._pick_worker(hash(f"cold-{cid}"))
        chosen.append(worker.idx)
        worker.enqueue(cid, {}, jd._Holder())
    assert chosen == [0, 1, 2, 3]


def test_serial_single_pick_still_worker_zero():
    d = _daemon(pool_size=4)
    d._workers = [_worker() for _ in range(4)]
    for i, w in enumerate(d._workers):
        w.idx = i
    # Cold family, all idle -> least-busy tie -> worker 0.
    assert d._pick_worker(hash("cold")).idx == 0


def _ready_worker(idx, stop):
    w = jd._Worker(idx, stop)
    w._ready.set()  # mark ws connected + prewarmed so sticky routing can engage
    return w


def test_pick_worker_same_family_sticks_to_ready_worker():
    d = _daemon(pool_size=3)
    stop = threading.Event()
    d._workers = [_ready_worker(i, stop) for i in range(3)]
    fh = hash("GRADE")
    first = d._pick_worker(fh)
    assert first.idx == 0  # cold -> least-busy -> lowest index
    # Second serial call (inflight 0) sticks to the same ready worker -> cache hit.
    assert d._pick_worker(fh).idx == 0
    # Same family still pinned to worker 0.
    assert d._family_to_worker[fh].idx == 0


def test_pick_worker_same_family_overflows_when_busy():
    d = _daemon(pool_size=3)
    stop = threading.Event()
    d._workers = [_ready_worker(i, stop) for i in range(3)]
    fh = hash("GRADE")
    assert d._pick_worker(fh).idx == 0  # cold -> pin to worker 0
    _register(d._workers[0], 1, jd._Holder())  # worker 0 now has 1 in-flight
    # Sticky worker is busy (inflight >= STICKY_OVERFLOW_INFLIGHT) -> overflow to
    # least-busy WITHOUT re-pinning, so parallelism is preserved.
    assert d._pick_worker(fh).idx == 1
    # The cache home is still worker 0 (no re-pin on overflow).
    assert d._family_to_worker[fh].idx == 0
    # Once worker 0 drains, the next serial same-family call sticks back to it.
    with d._workers[0]._state_lock:
        d._workers[0]._holders.clear()
    assert d._pick_worker(fh).idx == 0


def test_pick_worker_dead_sticky_repins_least_busy():
    d = _daemon(pool_size=3)
    stop = threading.Event()
    d._workers = [_ready_worker(i, stop) for i in range(3)]
    fh = hash("GRADE")
    assert d._pick_worker(fh).idx == 0  # pin to worker 0
    d._workers[0]._ready.clear()  # worker 0's ws died
    # Dead sticky worker -> cold path -> least-busy -> re-pin to a live worker.
    assert d._pick_worker(fh).idx == 1
    assert d._family_to_worker[fh].idx == 1


def test_pick_worker_multi_family_share_sticky_worker():
    # The inference machine caches EVERY prefix it has seen, so multiple families
    # pinned to the same sticky worker all hit. Two cold families both land on
    # worker 0 (least-busy, lowest index) and stay there.
    d = _daemon(pool_size=3)
    stop = threading.Event()
    d._workers = [_ready_worker(i, stop) for i in range(3)]
    fa, fb = hash("GRADE"), hash("GROUNDED")
    assert d._pick_worker(fa).idx == 0
    assert d._pick_worker(fb).idx == 0
    assert d._pick_worker(fa).idx == 0
    assert d._pick_worker(fb).idx == 0


def test_pick_worker_cold_pin_during_busy_sticks_to_alt_worker():
    # The real win over plain least-busy: a family whose FIRST call arrives while
    # worker 0 is busy cold-pins to a non-zero worker, and sticky keeps subsequent
    # calls on that cache home. Plain least-busy would scatter them back to
    # worker 0 (where this family was never cached) -> miss.
    d = _daemon(pool_size=3)
    stop = threading.Event()
    d._workers = [_ready_worker(i, stop) for i in range(3)]
    fh = hash("ARM")
    _register(d._workers[0], 1, jd._Holder())  # worker 0 busy when ARM first arrives
    assert d._pick_worker(fh).idx == 1  # cold -> least-busy worker 1, pin ARM->1
    with d._workers[0]._state_lock:  # worker 0 drains
        d._workers[0]._holders.clear()
    # Next ARM call sticks to worker 1 (its cache home), NOT worker 0.
    assert d._pick_worker(fh).idx == 1
    assert d._family_to_worker[fh].idx == 1


def test_pick_worker_sticky_off_falls_back_to_least_busy(monkeypatch):
    monkeypatch.setattr(jd, "STICKY_ROUTING", False)
    d = _daemon(pool_size=3)
    stop = threading.Event()
    d._workers = [_ready_worker(i, stop) for i in range(3)]
    fh = hash("ARM")
    _register(d._workers[0], 1, jd._Holder())
    assert d._pick_worker(fh).idx == 1  # least-busy, NO pin
    with d._workers[0]._state_lock:
        d._workers[0]._holders.clear()
    # No sticky home -> least-busy -> worker 0 (lowest index) -> would miss.
    assert d._pick_worker(fh).idx == 0
    assert d._family_to_worker == {}  # no pinning recorded when sticky is off


def test_submit_hashes_system_prompt_for_sticky_routing(monkeypatch):
    d = _daemon(pool_size=2)
    d._workers = [_worker(), _worker()]
    for i, w in enumerate(d._workers):
        w.idx = i
    seen: list[int] = []

    def fake_pick(family_hash: int):
        seen.append(family_hash)
        return d._workers[0]

    monkeypatch.setattr(d, "_pick_worker", fake_pick)

    def fast_enqueue(self, cid, req, holder):
        holder.done_args = '{"ok": true}'
        holder.event.set()

    monkeypatch.setattr(jd._Worker, "enqueue", fast_enqueue)

    d.submit({"system": "GRADE", "user": "x", "schema": {}}, timeout=1.0)
    d.submit({"system": "GRADE", "user": "y", "schema": {}}, timeout=1.0)
    d.submit({"system": "OTHER", "user": "z", "schema": {}}, timeout=1.0)
    assert seen[0] == seen[1]   # same system prompt -> same family hash
    assert seen[2] != seen[0]   # different system prompt -> different family hash


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


def test_drain_outbox_registers_holder_and_sends_response_create(monkeypatch):
    # Regression guard: the holder registration + response.create send MUST live
    # INSIDE the while-loop. A prior revert once de-indented them out of the loop,
    # making the send unreachable and silently dropping EVERY request (the daemon
    # drained the outbox and returned without ever calling OpenAI -> client
    # timeout). This test fails on that de-indent (sent=[] and holder unregistered).
    import types

    w = _worker()
    sent: list[dict] = []
    monkeypatch.setattr(jd.cj, "_send_text", lambda ws, obj: sent.append(obj))
    monkeypatch.setattr(jd.cj, "_response_create", lambda req, cid: {"sentinel": cid})
    h = jd._Holder()
    w.enqueue(7, {"system": "S", "user": "U", "schema": {}}, h)
    w._drain_outbox(types.SimpleNamespace())  # only passed through to _send_text
    assert sent == [{"sentinel": 7}]  # response.create sent exactly once for cid 7
    with w._state_lock:
        assert w._holders.get(7) is h  # holder registered under cid 7
    assert w._outbox.empty()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
