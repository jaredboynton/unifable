#!/usr/bin/env python3
"""Persistent per-session gpt-realtime-2 judge daemon (warm WebSocket).

One process per (session, cwd) holds a single authenticated Realtime WebSocket and
serves judge requests from stateless hook subprocesses over a unix domain socket.
Each request runs as an out-of-band response (``conversation:"none"``) on the warm
socket, so there is no per-call TLS+WS+auth handshake (the dominant judge latency)
and the stable instruction+schema prefix stays on one connection, maximizing
gpt-realtime-2 prompt-cache stickiness.

This daemon is a pure latency/cost optimization and is NEVER on the correctness
path: judge_client falls back to a direct ``codex_judge.ask_structured`` on any
daemon failure, unreachability, or timeout (the unifable fail-open prime directive).

Design notes:
  - A SINGLE I/O thread owns the SSL socket (Python's ssl module is not safe for
    concurrent read+write across threads). Client-handler threads only touch the
    unix socket, a queue, and per-request Events -- never the SSL socket.
  - Single instance per session via flock; idle-shuts-down after
    UNIFABLE_JUDGE_DAEMON_IDLE seconds; reconnects + refreshes the token on WS
    close / 1011 keepalive timeout / the 60-minute Realtime session cap.

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import select
import socket
import ssl
import sys
import threading
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import codex_judge as cj
from judge_ipc import recv_msg, send_msg
from judge_usage import parse_usage


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


IDLE_TTL = _env_float("UNIFABLE_JUDGE_DAEMON_IDLE", 120.0)
REQUEST_TIMEOUT = _env_float("UNIFABLE_JUDGE_DAEMON_REQUEST", 90.0)
FRAME_READ_TIMEOUT = _env_float("UNIFABLE_JUDGE_DAEMON_FRAME_TIMEOUT", 30.0)
# Max time select() blocks with no events; bounds how fast we notice _stop and
# service keepalive pongs. NOT a per-request poll: requests wake the loop via the
# self-pipe and ws frames wake it directly, both instantly.
STOP_TICK = _env_float("UNIFABLE_DAEMON_STOP_TICK", 5.0)
# Bounded wait for the whole pool to finish its initial connect+prewarm at
# startup so the first parallel batch never pays a handshake.
WARM_WAIT = _env_float("UNIFABLE_DAEMON_WARM_WAIT", 8.0)
MAX_INFLIGHT = cj.BATCH_MAX_INFLIGHT
# Sticky-overflow threshold (distinct from the hard MAX_INFLIGHT overload cap).
# A same-family call sticks to the worker that last served it (Realtime caches
# the instructions+tools prefix on that socket's inference machine; no
# prompt_cache_key in the WS API, verified 2026-06-27) ONLY while the sticky
# worker has fewer than this many in-flight. At/above it, overflow to least-busy
# so a same-family PARALLEL burst (e.g. the 4 mini-nav fanout) spreads across the
# pool instead of serializing onto one socket. Default 1 = overflow on any
# overlap, so serial same-family calls (which arrive at inflight 0) always stick
# and hit the cache, while parallel same-family calls spread.
STICKY_OVERFLOW_INFLIGHT = max(1, _env_int("UNIFABLE_STICKY_OVERFLOW_INFLIGHT", 1))
# Kill-switch for family-sticky routing. Default on. When off, _pick_worker
# ignores the family hash and falls back to ready-aware least-busy everywhere
# (the pre-sticky behavior, minus the dead-ws guard). Keep sticky on for Realtime
# prompt-cache locality; flip off only to A/B or roll back.
STICKY_ROUTING = _env_bool("UNIFABLE_STICKY_ROUTING", True)
# Pool size: number of independent warm sockets. The PostToolUse fan-out issues up
# to four concurrent judge requests per tool result; a single Realtime session
# serializes responses, so true parallelism comes from independent sessions. 4 is
# the measured ceiling of useful parallelism (docs/benchmarks/realtime-concurrency.md,
# 2026-06-25): a 4-socket pool beat 8 and 16 on both models, and more sockets only
# add connect contention. Serial callers (PreToolUse breaker, Stop) still land on
# worker 0 (least-busy, lowest-index tie-break), so gpt-realtime-2 prompt-cache
# stickiness is preserved. Override with UNIFABLE_DAEMON_POOL.
POOL_SIZE = max(1, _env_int("UNIFABLE_DAEMON_POOL", 4))


class _Holder:
    __slots__ = ("event", "args", "done_args", "text", "usage", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.args: list[str] = []
        self.done_args: str | None = None
        self.text: list[str] = []
        self.usage: dict[str, int] | None = None
        self.error: str | None = None


class _Worker:
    """One independent warm Realtime socket with its own io-thread, outbox, and
    holders map. The pool runs N of these; the daemon dispatches each request to
    the least-busy worker. A single Realtime session serializes responses, so
    true parallelism comes from spreading requests across workers (each its own
    session)."""

    def __init__(self, idx: int, stop: threading.Event) -> None:
        self.idx = idx
        self._stop = stop
        self._ws: ssl.SSLSocket | None = None
        self._state_lock = threading.Lock()
        self._holders: dict[int, _Holder] = {}
        self._rid_to_cid: dict[Any, int] = {}
        self._outbox: queue.Queue[tuple[int, dict, _Holder]] = queue.Queue()
        self._thread: threading.Thread | None = None
        # Self-pipe to wake the io-loop the instant a request is enqueued, so the
        # select() blocks on real events (ws frames OR new work) instead of
        # polling. Eliminates the per-request poll latency.
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._ready = threading.Event()  # set once the ws is connected + prewarmed

    def start(self) -> None:
        self._thread = threading.Thread(target=self._io_loop, name=f"rt-io-{self.idx}", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def inflight(self) -> int:
        with self._state_lock:
            return len(self._holders)

    def load(self) -> int:
        """Pending work on this worker: in-flight holders plus queued outbox items."""
        with self._state_lock:
            holders = len(self._holders)
        return holders + self._outbox.qsize()

    def idle(self) -> bool:
        return self.inflight() == 0 and self._outbox.empty()

    def ready(self) -> bool:
        """WebSocket connected + prewarmed (usable as a sticky route target)."""
        return self._ready.is_set()

    def enqueue(self, cid: int, req: dict[str, Any], holder: _Holder) -> None:
        self._outbox.put((cid, req, holder))
        try:
            self._wake_w.send(b"\x01")  # wake the io-loop immediately
        except OSError:
            pass

    # --- WS lifecycle (io-thread only) --------------------------------------
    def _ensure_ws(self) -> ssl.SSLSocket:
        if self._ws is not None:
            return self._ws
        try:
            tokens = cj._fresh_tokens(None, force=False)
            ws = cj._ws_connect(tokens, cj.MODEL, cj.HANDSHAKE_TIMEOUT)
        except cj.JudgeError as exc:
            if "handshake rejected" not in str(exc):
                raise
            tokens = cj._fresh_tokens(None, force=True)
            ws = cj._ws_connect(tokens, cj.MODEL, cj.HANDSHAKE_TIMEOUT)
        cj._send_text(
            ws,
            {"type": "session.update", "session": {"type": "realtime", "output_modalities": ["text"]}},
        )
        self._ws = ws
        self._ready.set()
        return ws

    def _mark_ws_dead(self) -> None:
        self._ready.clear()
        if self._ws is not None:
            try:
                self._ws.close()
            except OSError:
                pass
            self._ws = None
        self._fail_all("judge websocket closed")

    def _fail_all(self, reason: str) -> None:
        with self._state_lock:
            holders = list(self._holders.values())
            self._holders.clear()
            self._rid_to_cid.clear()
        for h in holders:
            if not h.event.is_set():
                h.error = reason
                h.event.set()

    def _drain_wake(self) -> None:
        try:
            while self._wake_r.recv(4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def _io_loop(self) -> None:
        # Block on the ws AND the wake pipe. The short STOP_TICK timeout only
        # bounds how fast we notice _stop / send keepalive pongs; it is NOT a
        # per-request poll (requests and ws frames both wake select instantly).
        while not self._stop.is_set():
            try:
                ws = self._ensure_ws()
            except Exception:
                self._fail_all("judge websocket unavailable")
                if self._stop.wait(1.0):
                    break
                continue
            try:
                self._drain_outbox(ws)
            except Exception:
                self._mark_ws_dead()
                continue
            try:
                readable, _, _ = select.select([ws, self._wake_r], [], [], STOP_TICK)
            except (OSError, ValueError):
                self._mark_ws_dead()
                continue
            if self._wake_r in readable:
                self._drain_wake()  # new request enqueued; loop will drain outbox
            if ws in readable or ws.pending():
                try:
                    self._read_available(ws)
                except Exception:
                    self._mark_ws_dead()
                    continue

    def _drain_outbox(self, ws: ssl.SSLSocket) -> None:
        while True:
            try:
                cid, req, holder = self._outbox.get_nowait()
            except queue.Empty:
                return
            with self._state_lock:
                self._holders[cid] = holder
            cj._send_text(ws, cj._response_create(req, cid))

    def _read_available(self, ws: ssl.SSLSocket) -> None:
        first = True
        while first or ws.pending():
            first = False
            ws.settimeout(FRAME_READ_TIMEOUT)
            opcode, payload = cj._read_message(ws)
            if opcode == 0x8:  # close
                raise OSError("websocket close frame")
            if opcode == 0x9:  # ping -> pong (keepalive; unanswered -> 1011 disconnect)
                ws.sendall(cj._encode_frame(payload, opcode=0xA))
                continue
            if opcode not in (0x0, 0x1, 0x2):
                continue
            try:
                env = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            self._route(env)

    # --- routing ------------------------------------------------------------
    def _cid_of(self, env: dict[str, Any]) -> int | None:
        resp = env.get("response")
        if isinstance(resp, dict):
            rid = resp.get("id")
            cid = (resp.get("metadata") or {}).get("cid")
            if rid is not None and cid is not None:
                try:
                    with self._state_lock:
                        self._rid_to_cid[rid] = int(cid)
                except (TypeError, ValueError):
                    pass
        rid = env.get("response_id")
        if rid is None and isinstance(env.get("response"), dict):
            rid = env["response"].get("id")
        with self._state_lock:
            return self._rid_to_cid.get(rid)

    def _route(self, env: dict[str, Any]) -> None:
        kind = env.get("type", "")
        if kind in ("error", "response.failed"):
            cid = self._cid_of(env)
            err = env.get("error") if kind == "error" else (env.get("response") or {}).get("error")
            detail = cj._provider_error(err)
            if cid is None:
                self._mark_ws_dead()  # session-level failure: clients fall back
                return
            self._finish(cid, error=detail)
            return
        cid = self._cid_of(env)
        if cid is None:
            return
        with self._state_lock:
            h = self._holders.get(cid)
        if h is None:
            return
        if kind == "response.function_call_arguments.delta":
            d = env.get("delta")
            if isinstance(d, str):
                h.args.append(d)
        elif kind == "response.function_call_arguments.done":
            a = env.get("arguments")
            if isinstance(a, str):
                h.done_args = a
        elif kind == "response.output_text.delta":
            d = env.get("delta")
            if isinstance(d, str):
                h.text.append(d)
        elif kind in ("response.done", "response.completed"):
            if h.done_args is None:
                h.done_args = cj._function_args_from_done(env)
            usage = parse_usage(env)
            if usage is not None:
                h.usage = usage
            self._finish(cid)

    def _finish(self, cid: int, error: str | None = None) -> None:
        with self._state_lock:
            h = self._holders.get(cid)
        if h is None:
            return
        if error is not None and h.error is None:
            h.error = error
        h.event.set()

    def discard(self, cid: int) -> None:
        with self._state_lock:
            self._holders.pop(cid, None)

    def close_ws(self) -> None:
        if self._ws is not None:
            try:
                self._ws.sendall(cj._encode_frame(b"", opcode=0x8))
                self._ws.close()
            except OSError:
                pass
            self._ws = None
        for s in (self._wake_r, self._wake_w):
            try:
                s.close()
            except OSError:
                pass


class JudgeDaemon:
    def __init__(self, session_key: str, sock_path: str, pool_size: int = POOL_SIZE) -> None:
        self.session_key = session_key
        self.sock_path = Path(sock_path)
        self._stop = threading.Event()
        self._pool_size = max(1, pool_size)
        self._workers: list[_Worker] = []
        self._dispatch_lock = threading.Lock()
        self._cid_counter = 0
        self._cid_to_worker: dict[int, _Worker] = {}
        self._last_activity = time.monotonic()
        self._srv: socket.socket | None = None
        self._lock_fh: Any = None
        # Sticky family->worker routing for Realtime prompt-cache locality.
        # Realtime caches the instructions+tools prefix on the specific inference
        # machine a socket is pinned to -- there is NO prompt_cache_key in the WS
        # API (verified 2026-06-27: 'session.prompt_cache_key' and
        # 'response.prompt_cache_key' both return unknown_parameter), so cross-call
        # cache hits require same-family calls to reuse the same WORKER SOCKET.
        # We stick a family (hash of its system prompt) to the worker that last
        # served it; overflow to least-busy when that worker is busy so a
        # same-family parallel burst still parallelizes (cache miss on the
        # overflow is the right tradeoff vs serializing onto one socket).
        self._family_to_worker: dict[int, _Worker] = {}
        self._family_lock = threading.Lock()

    # --- single instance ----------------------------------------------------
    def _acquire_singleton(self) -> bool:
        import fcntl

        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.sock_path.with_suffix(".lock")
        try:
            self._lock_fh = open(lock_path, "w")
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _bind(self) -> None:
        try:
            if self.sock_path.exists():
                self.sock_path.unlink()  # stale socket from a dead instance (we hold the lock)
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.sock_path))
        srv.listen(64)
        srv.settimeout(1.0)
        self._srv = srv

    def _least_busy(self) -> _Worker:
        # Prefer workers with a live (ready) websocket, then least pending work,
        # then lowest index. At cold start none are ready yet -> falls through to
        # load/idx (the io-loop connects the chosen worker on demand).
        return min(self._workers, key=lambda w: (not w.ready(), w.load(), w.idx))

    def _pick_worker(self, family_hash: int) -> _Worker:
        # Sticky-with-overflow for Realtime prompt-cache locality. Same family
        # (hash of the system prompt) prefers the worker that last served it ->
        # same socket -> cache hit on the instructions+tools prefix (the machine
        # caches every prefix it has seen, so multiple families sharing a sticky
        # worker all hit). When the sticky worker has >= STICKY_OVERFLOW_INFLIGHT
        # in-flight, overflow to least-busy WITHOUT re-pinning, so a same-family
        # parallel burst spreads across the pool instead of serializing, and the
        # sticky worker stays the cache home for the next serial call. Cold start
        # or dead sticky worker -> least-busy and pin it as the new home. When
        # STICKY_ROUTING is off, ignore the family hash (pre-sticky behavior).
        if STICKY_ROUTING:
            with self._family_lock:
                sticky = self._family_to_worker.get(family_hash)
            if sticky is not None and sticky.ready():
                if sticky.inflight() < STICKY_OVERFLOW_INFLIGHT:
                    return sticky
                return self._least_busy()
        chosen = self._least_busy()
        if STICKY_ROUTING:
            with self._family_lock:
                self._family_to_worker[family_hash] = chosen
        return chosen

    def submit(self, req: dict[str, Any], timeout: float) -> tuple[dict[str, Any], dict[str, int] | None]:
        with self._dispatch_lock:
            total_load = sum(w.load() for w in self._workers)
            if total_load >= MAX_INFLIGHT * self._pool_size:
                raise RuntimeError("judge daemon overloaded")
            self._cid_counter += 1
            cid = self._cid_counter
            family_hash = hash(req.get("system") or "")
            worker = self._pick_worker(family_hash)
            self._cid_to_worker[cid] = worker
        holder = _Holder()
        self._last_activity = time.monotonic()
        worker.enqueue(cid, req, holder)
        try:
            if not holder.event.wait(timeout):
                worker.discard(cid)
                raise TimeoutError("judge daemon request timed out")
        finally:
            worker.discard(cid)
            with self._dispatch_lock:
                self._cid_to_worker.pop(cid, None)
        self._last_activity = time.monotonic()
        if holder.error:
            raise RuntimeError(holder.error)
        chosen = holder.done_args or ("".join(holder.args) if holder.args else "") or "".join(holder.text)
        if not chosen or not chosen.strip():
            raise RuntimeError("realtime stream produced no structured output")
        obj = json.loads(chosen)
        if not isinstance(obj, dict):
            raise RuntimeError("output is not a json object")
        return obj, holder.usage

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(REQUEST_TIMEOUT + 10.0)
            msg = recv_msg(conn)
            if not isinstance(msg, dict):
                send_msg(conn, {"ok": False, "error": "bad request"})
                return
            req = {
                "system": str(msg.get("system") or ""),
                "user": str(msg.get("user") or ""),
                "schema": msg.get("schema") or {},
                "schema_name": str(msg.get("schema_name") or "result"),
            }
            re = msg.get("reasoning_effort")
            if re is not None and str(re).strip():
                req["reasoning_effort"] = str(re).strip()
            try:
                obj, usage = self.submit(req, REQUEST_TIMEOUT)
            except Exception as exc:
                send_msg(conn, {"ok": False, "error": str(exc)})
                return
            send_msg(conn, {"ok": True, "object": obj, "usage": usage or {}})
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _maybe_idle_shutdown(self) -> None:
        if all(w.idle() for w in self._workers) and (time.monotonic() - self._last_activity) > IDLE_TTL:
            self._stop.set()

    def _idle_monitor(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(1.0):
                break
            self._maybe_idle_shutdown()

    # --- lifecycle ----------------------------------------------------------
    def serve(self) -> int:
        if not self._acquire_singleton():
            return 0  # another daemon already owns this session
        try:
            self._bind()
        except OSError:
            return 0
        self._workers = [_Worker(i, self._stop) for i in range(self._pool_size)]
        for w in self._workers:
            w.start()
        # Eagerly warm the whole pool so EVERY socket is connected + prewarmed
        # before the first request, not lazily on first hit. A nudge wakes each
        # io-loop to run _ensure_ws immediately; we then wait (bounded) for all
        # sockets to report ready so a parallel batch never pays a handshake.
        for w in self._workers:
            try:
                w._wake_w.send(b"\x01")
            except OSError:
                pass
        warm_deadline = time.monotonic() + WARM_WAIT
        for w in self._workers:
            w.wait_ready(max(0.0, warm_deadline - time.monotonic()))
        threading.Thread(target=self._idle_monitor, name="rt-idle", daemon=True).start()
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = self._srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        self._stop.set()
        try:
            if self._srv is not None:
                self._srv.close()
        except OSError:
            pass
        for w in self._workers:
            w.close_ws()
        try:
            if self.sock_path.exists():
                self.sock_path.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--sock", required=True)
    parser.add_argument("--pool", type=int, default=POOL_SIZE)
    args = parser.parse_args(argv)
    try:
        return JudgeDaemon(args.session_key, args.sock, pool_size=args.pool).serve()
    except Exception:
        return 0  # never crash loudly; the client fails open to a direct call


if __name__ == "__main__":
    raise SystemExit(main())
