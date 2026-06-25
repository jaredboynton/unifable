"""WebSocket frame reassembly (RFC 6455 5.4) in codex_judge._read_message.

The Realtime server normally sends each event as one unfragmented frame, but a
fragmented message (FIN=0 data frame + continuation 0x0 frames) must be joined
into one payload rather than silently dropped at json.loads. Control frames
(ping) interleaved between fragments must be answered without losing reassembly.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

_GATE = Path(__file__).resolve().parents[1] / "scripts" / "gate"
if str(_GATE) not in sys.path:
    sys.path.insert(0, str(_GATE))

import codex_judge as cj


def _server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Build a server->client (unmasked) WebSocket frame."""
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
    """Serves a fixed byte stream via recv(); records sendall() (pongs)."""

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


def test_unfragmented_message_passthrough():
    payload = json.dumps({"type": "response.done"}).encode()
    sock = _FakeSock(_server_frame(0x1, payload, fin=True))
    opcode, out = cj._read_message(sock)
    assert opcode == 0x1
    assert json.loads(out.decode()) == {"type": "response.done"}


def test_fragmented_message_is_reassembled():
    full = json.dumps({"type": "response.function_call_arguments.done", "arguments": '{"verdict":1}'}).encode()
    mid = len(full) // 2
    stream = _server_frame(0x1, full[:mid], fin=False) + _server_frame(0x0, full[mid:], fin=True)
    sock = _FakeSock(stream)
    opcode, out = cj._read_message(sock)
    assert opcode == 0x1  # original data opcode, not the continuation 0x0
    assert out == full
    assert json.loads(out.decode())["arguments"] == '{"verdict":1}'


def test_three_way_fragmentation():
    full = b'{"type":"x","data":"' + b"A" * 500 + b'"}'
    a, b = len(full) // 3, 2 * len(full) // 3
    stream = (
        _server_frame(0x1, full[:a], fin=False)
        + _server_frame(0x0, full[a:b], fin=False)
        + _server_frame(0x0, full[b:], fin=True)
    )
    opcode, out = cj._read_message(_FakeSock(stream))
    assert opcode == 0x1
    assert out == full


def test_ping_between_fragments_is_ponged_and_reassembly_continues():
    full = json.dumps({"type": "response.done", "n": 7}).encode()
    mid = len(full) // 2
    stream = (
        _server_frame(0x1, full[:mid], fin=False)
        + _server_frame(0x9, b"hb", fin=True)  # ping interleaved between fragments
        + _server_frame(0x0, full[mid:], fin=True)
    )
    sock = _FakeSock(stream)
    opcode, out = cj._read_message(sock)
    assert opcode == 0x1
    assert json.loads(out.decode()) == {"type": "response.done", "n": 7}
    # The interleaved ping must have been answered with a pong (opcode 0xA).
    assert sock.sent, "ping between fragments was not ponged"
    assert (sock.sent[0][0] & 0x0F) == 0xA


def test_control_frames_pass_through():
    # Ping as a standalone message returns immediately (caller pongs).
    opcode, payload = cj._read_message(_FakeSock(_server_frame(0x9, b"p", fin=True)))
    assert opcode == 0x9 and payload == b"p"
    # Close passes through.
    opcode, _ = cj._read_message(_FakeSock(_server_frame(0x8, b"", fin=True)))
    assert opcode == 0x8
