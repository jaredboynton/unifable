#!/usr/bin/env python3
"""Wave 3 contract: the canonical Realtime transport lives in unifable_runtime
and codex_judge re-exports it byte-for-byte.

The WebSocket framing (encode/read/reassembly), the OAuth token lifecycle, and
the connection helpers were extracted from scripts/gate/codex_judge.py into
unifable_runtime.transport.realtime_ws; the pure session/batch routing into
realtime_session. These tests pin that:

  - codex_judge.<name> IS the transport object (same identity), so every existing
    monkeypatch of cj._ws_connect / cj._fresh_tokens / cj._encode_frame and every
    direct cj._read_message call keeps hitting the canonical implementation;
  - frame encode/decode round-trips through the extracted client (mask bit set on
    client frames, server frames unmasked, fragmentation reassembled);
  - JWT exp parsing and the "refresh only when expired" gate behave;
  - the public ask_structured / ask_structured_batch API is intact.
"""

from __future__ import annotations

import json
import struct
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATE = REPO / "scripts" / "gate"
for p in (str(REPO), str(GATE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import codex_judge as cj  # noqa: E402

from unifable_runtime.transport import realtime_session as rs  # noqa: E402
from unifable_runtime.transport import realtime_ws as ws  # noqa: E402

# --- the canonical module is the source of truth, codex_judge re-exports it ----


def test_codex_judge_reexports_transport_identity():
    # Same function objects -> a monkeypatch on cj.* patches the real transport,
    # and a direct cj._read_message call runs the canonical reassembly.
    assert cj._ws_connect is ws._ws_connect
    assert cj._fresh_tokens is ws._fresh_tokens
    assert cj._encode_frame is ws._encode_frame
    assert cj._read_message is ws._read_message
    assert cj._send_text is ws._send_text
    assert cj.JudgeError is ws.JudgeError
    assert cj._provider_error is rs.provider_error
    assert cj._function_args_from_done is rs.function_args_from_done
    assert cj._batch_route is rs.batch_route
    assert cj._collect_batch is rs.collect_batch
    assert cj._reask_reason_from_text is rs.reask_reason_from_text
    assert cj._reask_eligible is rs.reask_eligible


def test_public_api_present():
    for name in ("ask_structured", "ask_structured_batch", "render_structured_request", "JudgeError"):
        assert hasattr(cj, name), name
    # Constants the timeout-budget tests and daemon read off codex_judge.
    assert cj.HANDSHAKE_TIMEOUT == ws.HANDSHAKE_TIMEOUT
    assert cj.READ_TIMEOUT == ws.READ_TIMEOUT
    assert isinstance(cj.BATCH_MAX_INFLIGHT, int)


# --- client frame encode (RFC 6455 5.3: client frames MUST be masked) ----------


def _decode_client_frame(frame: bytes) -> tuple[bool, int, bytes]:
    b0, b1 = frame[0], frame[1]
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    off = 2
    if length == 126:
        length = struct.unpack(">H", frame[off : off + 2])[0]
        off += 2
    elif length == 127:
        length = struct.unpack(">Q", frame[off : off + 8])[0]
        off += 8
    assert masked, "client frames must set the mask bit"
    mask = frame[off : off + 4]
    off += 4
    body = bytes(b ^ mask[i % 4] for i, b in enumerate(frame[off : off + length]))
    return fin, opcode, body


def test_encode_frame_is_masked_and_round_trips():
    payload = json.dumps({"type": "response.create"}).encode()
    fin, opcode, body = _decode_client_frame(ws._encode_frame(payload, opcode=0x1))
    assert fin and opcode == 0x1
    assert body == payload


def test_encode_frame_extended_length_16bit():
    payload = b"x" * 1000  # >125 -> 16-bit length path
    frame = ws._encode_frame(payload, opcode=0x2)
    assert frame[1] & 0x7F == 126
    _, opcode, body = _decode_client_frame(frame)
    assert opcode == 0x2 and body == payload


def test_close_frame_opcode():
    _, opcode, body = _decode_client_frame(ws._encode_frame(b"", opcode=0x8))
    assert opcode == 0x8 and body == b""


# --- read-side reassembly (shared FakeSock; same contract as the fragment test) -


def _server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    b0 = (0x80 if fin else 0x00) | opcode
    n = len(payload)
    if n < 126:
        header = bytes([b0, n])
    elif n < 65536:
        header = bytes([b0, 126]) + struct.pack(">H", n)
    else:
        header = bytes([b0, 127]) + struct.pack(">Q", n)
    return header + payload


class _FakeSock:
    def __init__(self, data: bytes) -> None:
        self._buf = bytearray(data)
        self.sent: list[bytes] = []

    def recv(self, n: int) -> bytes:
        if not self._buf:
            raise OSError("eof")
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def settimeout(self, _t: float) -> None:
        pass


def test_close_frame_passes_through_read_message():
    opcode, payload = ws._read_message(_FakeSock(_server_frame(0x8, b"", fin=True)))
    assert opcode == 0x8 and payload == b""


def test_fragmented_reassembly_via_canonical_module():
    full = json.dumps({"type": "response.done", "k": "v" * 300}).encode()
    mid = len(full) // 2
    stream = _server_frame(0x1, full[:mid], fin=False) + _server_frame(0x0, full[mid:], fin=True)
    opcode, out = ws._read_message(_FakeSock(stream))
    assert opcode == 0x1 and out == full


# --- OAuth token lifecycle ----------------------------------------------------


def _jwt(exp_unix: int) -> str:
    import base64

    def seg(obj: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=")
        return raw.decode()

    return f"{seg({'alg': 'none'})}.{seg({'exp': exp_unix})}.sig"


def test_jwt_exp_parsing():
    assert ws._jwt_exp_unix(_jwt(1234567890)) == 1234567890
    assert ws._jwt_exp_unix("not-a-jwt") is None


def test_fresh_tokens_no_refresh_when_valid(tmp_path, monkeypatch):
    # A token expiring far in the future must NOT trigger a refresh (single-use
    # refresh_token must not be burned).
    auth = tmp_path / "auth.json"
    token = _jwt(int(time.time()) + 3600)
    auth.write_text(json.dumps({"tokens": {"access_token": token, "refresh_token": "r"}}))

    def boom(*a, **k):
        raise AssertionError("refresh must not be called for a valid token")

    monkeypatch.setattr(ws, "_refresh", boom)
    tokens = ws._fresh_tokens(str(auth))
    assert tokens["access_token"] == token


def test_fresh_tokens_refreshes_when_expired(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    expired = _jwt(int(time.time()) - 10)
    auth.write_text(json.dumps({"tokens": {"access_token": expired, "refresh_token": "r"}}))
    called = {"n": 0}

    def fake_refresh(doc, path):
        called["n"] += 1
        doc["tokens"]["access_token"] = "fresh"
        return doc

    monkeypatch.setattr(ws, "_refresh", fake_refresh)
    tokens = ws._fresh_tokens(str(auth))
    assert called["n"] == 1
    assert tokens["access_token"] == "fresh"


def test_fresh_tokens_missing_access_raises(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {}}))
    try:
        ws._fresh_tokens(str(auth))
    except cj.JudgeError as exc:
        assert "access_token" in str(exc)
    else:
        raise AssertionError("expected JudgeError for missing access_token")


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
