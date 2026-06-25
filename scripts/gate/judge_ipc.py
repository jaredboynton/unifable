#!/usr/bin/env python3
"""Length-prefixed JSON framing for the local judge-daemon unix socket.

Shared by judge_client (hook side) and realtime_daemon (server side). 4-byte
big-endian length header + UTF-8 JSON body, both directions. Stdlib only.
"""

from __future__ import annotations

import json
import struct
from typing import Any

_MAX_BYTES = 64 * 1024 * 1024  # judge payloads are <=256k chars; this is generous headroom


def _recv_exactly(conn: Any, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def send_msg(conn: Any, obj: dict[str, Any]) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    conn.sendall(struct.pack(">I", len(data)) + data)


def recv_msg(conn: Any, max_bytes: int = _MAX_BYTES) -> dict[str, Any] | None:
    header = _recv_exactly(conn, 4)
    if header is None:
        return None
    (n,) = struct.unpack(">I", header)
    if n <= 0 or n > max_bytes:
        return None
    body = _recv_exactly(conn, n)
    if body is None:
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None
