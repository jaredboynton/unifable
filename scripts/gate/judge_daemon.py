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


IDLE_TTL = _env_float("UNIFABLE_JUDGE_DAEMON_IDLE", 120.0)
REQUEST_TIMEOUT = _env_float("UNIFABLE_JUDGE_DAEMON_REQUEST", 90.0)
FRAME_READ_TIMEOUT = _env_float("UNIFABLE_JUDGE_DAEMON_FRAME_TIMEOUT", 30.0)
SELECT_POLL = 0.2
MAX_INFLIGHT = cj.BATCH_MAX_INFLIGHT


class _Holder:
    __slots__ = ("event", "args", "done_args", "text", "usage", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.args: list[str] = []
        self.done_args: str | None = None
        self.text: list[str] = []
        self.usage: dict[str, int] | None = None
        self.error: str | None = None


class JudgeDaemon:
    def __init__(self, session_key: str, sock_path: str) -> None:
        self.session_key = session_key
        self.sock_path = Path(sock_path)
        self._stop = threading.Event()
        self._ws: ssl.SSLSocket | None = None
        self._state_lock = threading.Lock()
        self._holders: dict[int, _Holder] = {}
        self._rid_to_cid: dict[Any, int] = {}
        self._cid_counter = 0
        self._outbox: queue.Queue[tuple[int, dict, _Holder]] = queue.Queue()
        self._last_activity = time.monotonic()
        self._srv: socket.socket | None = None
        self._lock_fh: Any = None

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

    # --- WS lifecycle (I/O thread only) -------------------------------------
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
        return ws

    def _mark_ws_dead(self) -> None:
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

    # --- I/O thread: interleave queued writes and frame reads ---------------
    def _io_loop(self) -> None:
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
                readable, _, _ = select.select([ws], [], [], SELECT_POLL)
            except (OSError, ValueError):
                self._mark_ws_dead()
                continue
            if readable or ws.pending():
                try:
                    self._read_available(ws)
                except Exception:
                    self._mark_ws_dead()
                    continue
            self._maybe_idle_shutdown()

    def _drain_outbox(self, ws: ssl.SSLSocket) -> None:
        while True:
            try:
                cid, req, holder = self._outbox.get_nowait()
            except queue.Empty:
                return
            with self._state_lock:
                self._holders[cid] = holder
            cj._send_text(ws, cj._response_create(req, cid))
            self._last_activity = time.monotonic()

    def _read_available(self, ws: ssl.SSLSocket) -> None:
        first = True
        while first or ws.pending():
            first = False
            ws.settimeout(FRAME_READ_TIMEOUT)
            opcode, payload = cj._read_frame(ws)
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

    # --- submit (client-handler threads) ------------------------------------
    def submit(self, req: dict[str, Any], timeout: float) -> tuple[dict[str, Any], dict[str, int] | None]:
        with self._state_lock:
            if len(self._holders) >= MAX_INFLIGHT:
                raise RuntimeError("judge daemon overloaded")
            self._cid_counter += 1
            cid = self._cid_counter
        holder = _Holder()
        self._outbox.put((cid, req, holder))
        if not holder.event.wait(timeout):
            with self._state_lock:
                self._holders.pop(cid, None)
            raise TimeoutError("judge daemon request timed out")
        with self._state_lock:
            self._holders.pop(cid, None)
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
        with self._state_lock:
            inflight = len(self._holders)
        if inflight == 0 and self._outbox.empty() and (time.monotonic() - self._last_activity) > IDLE_TTL:
            self._stop.set()

    # --- lifecycle ----------------------------------------------------------
    def serve(self) -> int:
        if not self._acquire_singleton():
            return 0  # another daemon already owns this session
        try:
            self._bind()
        except OSError:
            return 0
        io_thread = threading.Thread(target=self._io_loop, name="judge-io", daemon=True)
        io_thread.start()
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
        if self._ws is not None:
            try:
                self._ws.sendall(cj._encode_frame(b"", opcode=0x8))
                self._ws.close()
            except OSError:
                pass
            self._ws = None
        try:
            if self.sock_path.exists():
                self.sock_path.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-key", required=True)
    parser.add_argument("--sock", required=True)
    args = parser.parse_args(argv)
    try:
        return JudgeDaemon(args.session_key, args.sock).serve()
    except Exception:
        return 0  # never crash loudly; the client fails open to a direct call


if __name__ == "__main__":
    raise SystemExit(main())
