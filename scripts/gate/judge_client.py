#!/usr/bin/env python3
"""Hook-side client for the per-session judge daemon.

`daemon_ask` connects to (or lazily spawns) the session's judge daemon and runs
one structured judge over the warm WebSocket. It NEVER raises for an operational
failure: it returns ``(None, None)`` so judge_transport falls back to a direct
``codex_judge.ask_structured`` (the unifable fail-open prime directive).

Stdlib only.

# cleanup-traps: not-applicable -- detached session daemon (start_new_session)
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


CONNECT_TIMEOUT = _env_float("UNIFABLE_JUDGE_DAEMON_CONNECT", 0.5)
SPAWN_WAIT = _env_float("UNIFABLE_JUDGE_DAEMON_SPAWN_WAIT", 3.0)
REQUEST_TIMEOUT = _env_float("UNIFABLE_JUDGE_DAEMON_REQUEST", 95.0)


def _daemon_dir() -> Path:
    from ledger import data_root

    return data_root() / "judged"


def _sock_path(session_key: str) -> Path:
    return _daemon_dir() / f"{session_key}.sock"


def _session_key(input_data: dict[str, Any]) -> str:
    from ledger import ledger_key

    return ledger_key(input_data)


def _connect(path: Path, timeout: float) -> socket.socket:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    conn.connect(str(path))
    return conn


def _spawn(session_key: str, path: Path) -> None:
    daemon = _HERE / "realtime_daemon.py"
    try:
        devnull = open(os.devnull, "wb")
    except OSError:
        devnull = None
    try:
        subprocess.Popen(
            [sys.executable, str(daemon), "--session-key", session_key, "--sock", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=devnull or subprocess.DEVNULL,
            stderr=devnull or subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd=str(_HERE),
        )
    except Exception:
        pass


def _connect_or_spawn(session_key: str, path: Path) -> socket.socket | None:
    try:
        return _connect(path, CONNECT_TIMEOUT)
    except OSError:
        pass
    try:
        _daemon_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    _spawn(session_key, path)
    deadline = time.monotonic() + SPAWN_WAIT
    while time.monotonic() < deadline:
        time.sleep(0.05)
        try:
            return _connect(path, CONNECT_TIMEOUT)
        except OSError:
            continue
    return None


def daemon_ask(
    input_data: dict[str, Any],
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    schema_name: str = "result",
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[dict[str, Any] | None, dict[str, int] | None]:
    """Run one structured judge via the daemon. (None, None) signals fallback."""
    from judge_ipc import recv_msg, send_msg

    session_key = _session_key(input_data)
    path = _sock_path(session_key)
    conn = _connect_or_spawn(session_key, path)
    if conn is None:
        return None, None
    try:
        conn.settimeout(timeout)
        send_msg(
            conn,
            {"v": 1, "system": system, "user": user, "schema": schema, "schema_name": schema_name},
        )
        resp = recv_msg(conn)
    except OSError:
        return None, None
    finally:
        try:
            conn.close()
        except OSError:
            pass
    if not isinstance(resp, dict) or not resp.get("ok"):
        return None, None
    obj = resp.get("object")
    if not isinstance(obj, dict):
        return None, None
    usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else None
    return obj, usage
