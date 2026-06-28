#!/usr/bin/env python3
"""Canonical RFC 6455 WebSocket client + Codex OAuth for the Realtime judge.

Stdlib only: socket + ssl (WebSocket), urllib (OAuth refresh), base64/struct/json.
Speaks the OpenAI Realtime WebSocket protocol to
``wss://api.openai.com/v1/realtime?model=<model>``, authenticated with the Codex
ChatGPT OAuth bearer in ``~/.codex/auth.json`` (tokens.access_token +
chatgpt-account-id + originator: codex_cli_rs) -- no platform OPENAI_API_KEY, no
TLS fingerprint.

This module is host-agnostic: it carries no gate/transcript coupling. The gate
adapter ``scripts/gate/codex_judge.py`` re-exports these names so existing call
sites and tests (which monkeypatch ``cj._ws_connect`` / ``cj._fresh_tokens`` and
build frames with ``cj._encode_frame``) keep working unchanged.

Mirrors the cse-tools ``cse-realtime`` transport (crates/cse-realtime/src/*.rs).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --- Realtime + codex OAuth constants (mirror crates/cse-realtime/src/*.rs) ---
REALTIME_HOST = "api.openai.com"
REALTIME_PATH = "/v1/realtime"  # ?model=<model> appended (protocol.rs/lib.rs)
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"  # auth.rs:33
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # auth.rs:35
OAUTH_SCOPE = "openid profile email"  # auth.rs:37
ORIGINATOR = "codex_cli_rs"  # protocol.rs build_request


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# Judge deadlines are reconciled with the host Stop-hook budget (hooks.json wires
# gate_stop with timeout 120s). Worst case = handshake + read, kept comfortably
# under the host budget so the hook returns cleanly instead of being killed
# mid-judge (the codex-thread 10s timeout). See tests/test_stop_timeout_budget.py.
HANDSHAKE_TIMEOUT = _env_float("UNIFABLE_JUDGE_HANDSHAKE", 15.0)
READ_TIMEOUT = _env_float("UNIFABLE_JUDGE_TIMEOUT", 90.0)
REFRESH_TIMEOUT = _env_float("UNIFABLE_JUDGE_REFRESH_TIMEOUT", 15.0)


class JudgeError(Exception):
    """Any auth / transport / protocol failure talking to the Realtime API."""


# ---------------------------------------------------------------------------
# Auth: load ~/.codex/auth.json, refresh the access_token when stale (auth.rs)
# ---------------------------------------------------------------------------


def _auth_path(override: str | os.PathLike[str] | None) -> Path:
    if override:
        return Path(override)
    return Path.home() / ".codex" / "auth.json"


def _jwt_exp_unix(token: str) -> int | None:
    """exp (unix) from a JWT payload segment; None if unparseable (auth.rs:379)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".refresh.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _refresh(doc: dict[str, Any], path: Path) -> dict[str, Any]:
    """Mint a fresh token set via the OAuth refresh grant; write back (auth.rs:157)."""
    tokens = doc.get("tokens") or {}
    refresh_token = tokens.get("refresh_token") or ""
    if not refresh_token:
        raise JudgeError("no refresh_token in auth.json")
    body = json.dumps(
        {
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": OAUTH_SCOPE,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        method="POST",
        headers={"content-type": "application/json", "user-agent": ORIGINATOR},
    )
    try:
        with urllib.request.urlopen(req, timeout=REFRESH_TIMEOUT) as resp:
            v = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            j = json.loads(exc.read().decode("utf-8", "replace"))
            err = j.get("error") if isinstance(j.get("error"), dict) else j
            detail = str((err or {}).get("code") or (err or {}).get("message") or "")
        except Exception:
            pass
        if "reuse" in detail.lower() or "already been used" in detail.lower():
            raise JudgeError(
                "codex token refresh failed (refresh_token already used); run `codex login` to re-authenticate"
            ) from exc
        raise JudgeError(f"codex token refresh failed: HTTP {exc.code} {detail}".rstrip()) from exc
    except Exception as exc:  # noqa: BLE001
        raise JudgeError(f"token refresh failed: {exc}") from exc
    access = v.get("access_token")
    if not access:
        raise JudgeError("refresh response missing access_token")
    new_id = v.get("id_token") or ""
    new_refresh = v.get("refresh_token") or refresh_token
    tokens["access_token"] = access
    if new_id:
        tokens["id_token"] = new_id
    tokens["refresh_token"] = new_refresh
    doc["tokens"] = tokens
    doc["last_refresh"] = datetime.now(UTC).isoformat()
    _atomic_write(path, json.dumps(doc, indent=2))
    return doc


def _fresh_tokens(auth_path: str | os.PathLike[str] | None, force: bool = False) -> dict[str, Any]:
    path = _auth_path(auth_path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JudgeError(f"cannot read {path}: {exc}") from exc
    tokens = doc.get("tokens") or {}
    access = tokens.get("access_token")
    if not access:
        raise JudgeError("auth.json missing tokens.access_token")
    # Refresh only when the bearer access_token is actually expired (or forced by a
    # reactive handshake retry). The Realtime API accepts a valid access_token; the
    # refresh_token is single-use, so refreshing eagerly would burn it.
    exp = _jwt_exp_unix(access)
    if force or (exp is not None and exp - int(time.time()) <= 60):
        doc = _refresh(doc, path)
        tokens = doc.get("tokens") or {}
    return tokens


# ---------------------------------------------------------------------------
# Minimal RFC 6455 WebSocket client over stdlib socket + ssl
# ---------------------------------------------------------------------------


def _read_exactly(sock: ssl.SSLSocket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise JudgeError("websocket closed mid-frame")
        buf += chunk
    return bytes(buf)


def _encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Client->server frame; clients MUST mask (RFC 6455 5.3)."""
    out = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        out.append(0x80 | n)
    elif n < 65536:
        out.append(0x80 | 126)
        out += struct.pack(">H", n)
    else:
        out.append(0x80 | 127)
        out += struct.pack(">Q", n)
    mask = os.urandom(4)
    out += mask
    out += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(out)


def _read_frame(sock: ssl.SSLSocket) -> tuple[bool, int, bytes]:
    """Read one server frame -> (fin, opcode, payload). Server frames are unmasked."""
    b0, b1 = _read_exactly(sock, 2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exactly(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exactly(sock, 8))[0]
    mask = _read_exactly(sock, 4) if masked else b""
    payload = _read_exactly(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


def _read_message(sock: ssl.SSLSocket) -> tuple[int, bytes]:
    """Read one logical message, reassembling fragments (RFC 6455 5.4).

    Returns (opcode, payload). Control frames (close 0x8 / ping 0x9 / pong 0xA)
    are never fragmented and pass straight through for the caller to handle. A
    data message split across a non-FIN frame + continuation (0x0) frames is
    joined into one payload; a ping arriving between fragments is answered inline
    so reassembly can continue, and a close ends it.
    """
    fin, opcode, payload = _read_frame(sock)
    if opcode in (0x8, 0x9, 0xA) or fin:
        return opcode, payload
    data_opcode = opcode  # 0x1 text / 0x2 binary; continuations carry 0x0
    chunks = [payload]
    while True:
        fin, opcode, payload = _read_frame(sock)
        if opcode == 0x8:  # close mid-message
            return opcode, payload
        if opcode == 0x9:  # ping mid-message -> pong, keep reading
            sock.sendall(_encode_frame(payload, opcode=0xA))
            continue
        if opcode == 0xA:  # pong -> ignore
            continue
        chunks.append(payload)
        if fin:
            return data_opcode, b"".join(chunks)


def _send_text(sock: ssl.SSLSocket, obj: Any) -> None:
    sock.sendall(_encode_frame(json.dumps(obj).encode("utf-8"), opcode=0x1))


def _ws_connect(tokens: dict[str, Any], model: str, timeout: float) -> ssl.SSLSocket:
    path = f"{REALTIME_PATH}?model={model}"
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {REALTIME_HOST}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        f"authorization: Bearer {tokens['access_token']}",
    ]
    if tokens.get("account_id"):
        lines.append(f"chatgpt-account-id: {tokens['account_id']}")
    lines += [f"originator: {ORIGINATOR}", "", ""]
    request = "\r\n".join(lines).encode("ascii")

    ctx = ssl.create_default_context()
    raw = socket.create_connection((REALTIME_HOST, 443), timeout=timeout)
    sock = ctx.wrap_socket(raw, server_hostname=REALTIME_HOST)
    sock.sendall(request)

    resp = bytearray()
    sock.settimeout(timeout)
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            break
        resp += chunk
        if len(resp) > 65536:
            break
    status_line = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if "101" not in status_line:
        sock.close()
        raise JudgeError(f"websocket handshake rejected: {status_line}")
    return sock
