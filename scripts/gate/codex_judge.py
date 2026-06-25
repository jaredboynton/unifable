#!/usr/bin/env python3
"""gpt-realtime-2 structured-output client (stdlib only).

A Python port of the cse-tools `cse-realtime` Realtime transport
(crates/cse-realtime/src/{auth,protocol,lib}.rs). It speaks the OpenAI Realtime
WebSocket protocol to `wss://api.openai.com/v1/realtime?model=gpt-realtime-2`,
authenticated with the **Codex ChatGPT OAuth bearer** in `~/.codex/auth.json`
(tokens.access_token + chatgpt-account-id + originator: codex_cli_rs) -- no
platform OPENAI_API_KEY, no TLS fingerprint.

Structured output rides a single function tool with `tool_choice: "required"`:
session.update (instructions + the schema as a function tool) -> conversation.item
.create (the question) -> response.create -> read frames -> the function call's
arguments are the structured object.

Used by the unifable spec gate to have gpt-realtime-2 critically judge whether a
task's check output actually validates the task (verdict 1=validated/0=fail), so
"validated" cannot be faked by typing evidence text.

gpt-realtime-2 caps each message field at 256,000 characters. ask_structured
applies cap_judge_message() to system and user text before sending.

Judge/breaker Realtime sessions set ``reasoning.effort`` (default ``low`` via
``UNIFABLE_JUDGE_REASONING_EFFORT``), matching explore's explore-phase default.

Public API:
    ask_structured(system, user, schema, *, schema_name="result", model=MODEL,
                   auth_path=None, timeout=180.0) -> dict
    render_structured_request(system, user, schema, *, schema_name="result")
                   -> dict
    JudgeError -- raised on any auth/transport/protocol failure.

Stdlib only: socket + ssl (WebSocket), urllib (OAuth refresh), base64/struct/json.
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
from collections.abc import Callable
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

# gpt-realtime-2 over the Realtime API, authenticated with the Codex ChatGPT
# OAuth bearer (tokens.access_token in ~/.codex/auth.json) -- the same path
# cse-tools uses, no platform API key. Override with UNIFABLE_JUDGE_MODEL.
MODEL = os.environ.get("UNIFABLE_JUDGE_MODEL", "gpt-realtime-2")
REASONING_EFFORT = (os.environ.get("UNIFABLE_JUDGE_REASONING_EFFORT") or "low").strip() or "low"

_QUESTION_PREFIX = "QUESTION: "


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
# Ceiling on out-of-band responses launched on one socket per batch; larger
# batches are chunked so no socket exceeds it. Set from measurement, not a guess:
# a single socket runs ~192 concurrent out-of-band responses with zero loss and
# starts dropping at ~224. The Realtime API never hard-caps or rate-errors these
# (it "finishes" every response.create up to 272+); the failure is SILENT -- a
# completed response with empty structured output (the model skips the required
# tool call under load), which ask_structured_batch maps to a per-slot JudgeError.
# So we cap below the 224 degradation onset: 128 was 100% clean across repeated
# samples, leaving wide margin. Probabilistic with high variance, hence the margin.
# See docs: https://developers.openai.com/api/docs/guides/realtime-conversations
BATCH_MAX_INFLIGHT = int(os.environ.get("UNIFABLE_JUDGE_BATCH_MAX") or 128)

_HANDSHAKE_TIMEOUT = HANDSHAKE_TIMEOUT  # back-compat alias

# --- Reask on malformed output ------------------------------------------------
# gpt-realtime-2 occasionally emits a malformed structured response under load:
# empty function-call arguments (the model skips the required tool call), a
# wrong-shape object, invalid JSON, or a per-response ``response.failed``. These
# are not transport/auth failures -- the response completed, the content is just
# bad -- so feeding the reason back and re-issuing the request almost always
# recovers on the next try. This mirrors the explore skill submit-phase reask
# loop (scripts/realtime-trace.mjs runSubmitPhase, ``reask`` default 1) and the
# documented Realtime recovery of re-issuing ``response.create`` after a failed
# response. One reask only; bounded by the per-call deadline so it can never
# double the host Stop-hook budget.
REASK_ENABLED = (os.environ.get("UNIFABLE_JUDGE_REASK") or "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
REASK_ATTEMPTS = max(0, int(os.environ.get("UNIFABLE_JUDGE_REASK_ATTEMPTS") or 1))
# Minimum seconds that must remain on the deadline before starting a reask
# (a direct-path reask pays a fresh TLS+WS+auth handshake). Skip otherwise and
# let the malformed output surface, so the caller's fail-open handles it.
REASK_FLOOR = _env_float("UNIFABLE_JUDGE_REASK_FLOOR", 10.0)

# Error-message fragments that mean the failure is operational (not bad output)
# and must NOT be reasked -- re-issuing cannot help and a daemon reask on a dead
# socket is wasted work (the caller falls back to a direct call instead).
_DIRECT_INELIGIBLE = ("handshake rejected",)  # has its own force-refresh retry
_DAEMON_INELIGIBLE = (
    "judge websocket closed",
    "judge websocket unavailable",
    "timed out",  # RuntimeError/TimeoutError from submit -- let caller fall back
)


def _reask_eligible(err_msg: str, ineligible: tuple[str, ...]) -> bool:
    """True if a failure message looks like malformed output (worth one reask).

    Operational failures whose fragments appear in ``ineligible`` return False so
    the caller skips the reask and surfaces the error for fail-open fallback."""
    low = (err_msg or "").lower()
    return not any(frag.lower() in low for frag in ineligible)


def _reask_reason_from_text(text: str) -> str | None:
    """Classify captured structured output: None if it is a JSON object, else a
    short reason string (empty / invalid JSON / not an object)."""
    stripped = (text or "").strip()
    if not stripped:
        return "realtime stream produced no structured output"
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return f"output is not valid json: {exc}"
    if not isinstance(obj, dict):
        return "output is not a json object"
    return None


def _augment_user_text(user_text: str, reason: str) -> str:
    """Append the failure reason to the user text so the reask sees it (explore's
    ``PREVIOUS SUBMIT FAILED`` pattern). Left uncapped; the sender caps per the
    256k field limit before transmit."""
    return (
        f"{user_text}\n\nPREVIOUS JUDGE CALL FAILED: {reason}\n"
        "Return the complete valid object again, calling the tool exactly once."
    )


def _realtime_reasoning_config(effort: str | None = None) -> dict[str, Any]:
    """Realtime `reasoning.effort` for gpt-realtime-2 judge/breaker calls."""
    e = (effort or REASONING_EFFORT).strip() or "low"
    return {"reasoning": {"effort": e}}


def _realtime_reasoning_config(effort: str | None = None) -> dict[str, Any]:
    """Realtime `reasoning.effort` for gpt-realtime-2 judge/breaker calls."""
    e = (effort or REASONING_EFFORT).strip() or "low"
    return {"reasoning": {"effort": e}}


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


# ---------------------------------------------------------------------------
# Realtime structured ask (function tool + tool_choice=required)
# ---------------------------------------------------------------------------


def _provider_error(err: Any) -> str:
    if not isinstance(err, dict):
        return "provider error (no detail)"
    code = err.get("code") or err.get("type") or "unknown"
    return f"{code}: {err.get('message') or '(no detail)'}"


def _function_args_from_done(env: dict[str, Any]) -> str | None:
    """Pull a function_call's arguments string from a response.done envelope."""
    items: list[Any] = []
    resp = env.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("output"), list):
        items += resp["output"]
    if isinstance(env.get("output"), list):
        items += env["output"]
    for item in items:
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("arguments"):
            return str(item["arguments"])
    return None


def render_structured_request(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    schema_name: str = "result",
) -> dict[str, dict[str, Any]]:
    """Render the exact Realtime events used by ask_structured."""
    from transcript_tail import JUDGE_EFFECTIVE_MAX_CHARS, cap_judge_message

    system = cap_judge_message(system, JUDGE_EFFECTIVE_MAX_CHARS)
    user_cap = JUDGE_EFFECTIVE_MAX_CHARS - len(_QUESTION_PREFIX)
    user = cap_judge_message(user, user_cap)
    tool = {
        "type": "function",
        "name": schema_name,
        "description": "Return the structured result. Call exactly once with the complete object.",
        "parameters": schema,
    }
    return {
        "session.update": {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": system,
                "output_modalities": ["text"],
                "tools": [tool],
                "tool_choice": "required",
                **_realtime_reasoning_config(),
            },
        },
        "conversation.item.create": {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"{_QUESTION_PREFIX}{user}"}],
            },
        },
        "response.create": {
            "type": "response.create",
            "response": {"output_modalities": ["text"]},
        },
    }


def ask_structured(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    schema_name: str = "result",
    model: str = MODEL,
    auth_path: str | os.PathLike[str] | None = None,
    timeout: float = READ_TIMEOUT,
    on_usage: Callable[[dict[str, int]], None] | None = None,
    reask: bool | None = None,
) -> dict[str, Any]:
    """Ask gpt-realtime-2 for one structured object via a required function tool.

    Returns the parsed object. Raises JudgeError on any failure (so callers fail
    safe). Refreshes the access_token if expired, and retries once on a handshake
    auth rejection after forcing a refresh (protocol.rs run_session_structured).

    When ``on_usage`` is given it is called once with the parsed token-usage record
    from ``response.done`` (input/output/cached/total tokens) so callers can track
    prompt-cache effectiveness. Never raises out of the usage path.

    On malformed structured output (empty args, invalid JSON, not an object, or a
    per-response ``response.failed``) the failure reason is fed back and the
    request re-issued, up to ``REASK_ATTEMPTS`` extra tries (default 1) when
    ``reask`` is True/None-and-enabled. Each attempt shares one per-call deadline
    so reasking can never exceed the caller's ``timeout`` (and the host Stop-hook
    budget). Operational failures (handshake rejection, websocket closed) are not
    reasked -- they have their own recovery or surface for fail-open fallback."""
    from transcript_tail import JUDGE_EFFECTIVE_MAX_CHARS, cap_judge_message

    rendered = render_structured_request(system, user, schema, schema_name=schema_name)
    session_update = rendered["session.update"]
    question = rendered["conversation.item.create"]
    response_create = rendered["response.create"]
    user_cap = JUDGE_EFFECTIVE_MAX_CHARS - len(_QUESTION_PREFIX)

    if reask is None:
        reask = REASK_ENABLED
    attempts = 1 + (REASK_ATTEMPTS if reask else 0)
    deadline = time.monotonic() + timeout
    user_text = user
    attempt = 0
    while True:
        try:
            text = _ask_once(
                auth_path,
                model,
                session_update,
                question,
                response_create,
                user_text,
                user_cap,
                deadline,
                on_usage,
            )
        except JudgeError as exc:
            msg = str(exc)
            if "handshake rejected" in msg:
                # token may be stale; force-refresh + retry once (independent of reask)
                text = _ask_once(
                    auth_path,
                    model,
                    session_update,
                    question,
                    response_create,
                    user_text,
                    user_cap,
                    deadline,
                    on_usage,
                    force_refresh=True,
                )
            elif (
                attempt + 1 < attempts
                and _reask_eligible(msg, _DIRECT_INELIGIBLE)
                and time.monotonic() < deadline - REASK_FLOOR
            ):
                user_text = _augment_user_text(user_text, msg)
                attempt += 1
                continue
            else:
                raise
        reason = _reask_reason_from_text(text)
        if reason is None:
            return json.loads(text.strip())
        if attempt + 1 < attempts and time.monotonic() < deadline - REASK_FLOOR:
            user_text = _augment_user_text(user_text, reason)
            attempt += 1
            continue
        raise JudgeError(reason)


def _ask_once(
    auth_path: str | os.PathLike[str] | None,
    model: str,
    session_update: dict[str, Any],
    question: dict[str, Any],
    response_create: dict[str, Any],
    user_text: str,
    user_cap: int,
    deadline: float,
    on_usage: Callable[[dict[str, int]], None] | None,
    *,
    force_refresh: bool = False,
) -> str:
    """One connect + session.update + ask + read, returning the captured args/text.

    Shared by each attempt of ``ask_structured`` (and its handshake-refresh retry).
    ``user_text`` is capped per the 256k field limit and written into ``question``
    before transmit so a reask can substitute augmented text. Raises JudgeError on
    any auth/transport/protocol failure or empty structured output."""
    from transcript_tail import cap_judge_message

    tokens = _fresh_tokens(auth_path, force=force_refresh)
    sock = _ws_connect(tokens, model, _HANDSHAKE_TIMEOUT)
    try:
        question["item"]["content"][0]["text"] = f"{_QUESTION_PREFIX}{cap_judge_message(user_text, user_cap)}"
        _send_text(sock, session_update)
        _send_text(sock, question)
        _send_text(sock, response_create)
        args_buf: list[str] = []
        args_done: str | None = None
        text_buf: list[str] = []
        while time.monotonic() < deadline:
            opcode, payload = _read_message(sock)
            if opcode == 0x8:  # close
                break
            if opcode == 0x9:  # ping -> pong
                sock.sendall(_encode_frame(payload, opcode=0xA))
                continue
            if opcode not in (0x0, 0x1, 0x2):
                continue
            try:
                env = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # never log raw frames (may carry bearer material)
            kind = env.get("type", "")
            if kind == "response.output_text.delta":
                d = env.get("delta")
                if isinstance(d, str):
                    text_buf.append(d)
            elif kind == "response.function_call_arguments.delta":
                d = env.get("delta")
                if isinstance(d, str):
                    args_buf.append(d)
            elif kind == "response.function_call_arguments.done":
                a = env.get("arguments")
                if isinstance(a, str):
                    args_done = a
            elif kind in ("error", "response.failed"):
                err = env.get("error") if kind == "error" else (env.get("response") or {}).get("error")
                raise JudgeError(_provider_error(err))
            elif kind in ("response.done", "response.completed"):
                args_done = args_done or _function_args_from_done(env)
                if on_usage is not None:
                    try:
                        from judge_usage import parse_usage

                        usage = parse_usage(env)
                        if usage is not None:
                            on_usage(usage)
                    except Exception:
                        pass  # usage is best-effort; never fail the judge
                break
        chosen = args_done or ("".join(args_buf) if args_buf else "") or "".join(text_buf)
        if not chosen.strip():
            raise JudgeError("realtime stream produced no structured output")
        return chosen
    finally:
        try:
            sock.sendall(_encode_frame(b"", opcode=0x8))
            sock.close()
        except OSError:
            pass



# ---------------------------------------------------------------------------
# Concurrent batch: many structured asks over ONE WebSocket (out-of-band responses)
#
# Each request becomes a response.create with conversation:"none" (out of band, so
# responses run in parallel and never write the shared conversation), its own
# instructions/tools/tool_choice/input, and metadata {cid} for correlation. Per the
# Realtime docs, multiple responses may be created in parallel and disambiguated by
# metadata; we map response.id -> cid at response.created and route every later
# frame (function_call_arguments.*, output_text.delta, response.done) by response_id.
# ---------------------------------------------------------------------------


def _new_batch_state(n: int) -> dict[str, Any]:
    return {
        "n": n,
        "rid_to_cid": {},
        "args": {i: [] for i in range(n)},
        "done_args": dict.fromkeys(range(n)),
        "text": {i: [] for i in range(n)},
        "error": dict.fromkeys(range(n)),
        "finished": set(),
        "session_error": None,
    }


def _register_metadata(state: dict[str, Any], env: dict[str, Any]) -> None:
    resp = env.get("response")
    if not isinstance(resp, dict):
        return
    rid = resp.get("id")
    meta = resp.get("metadata") or {}
    cid = meta.get("cid")
    if rid is not None and cid is not None:
        try:
            state["rid_to_cid"][rid] = int(cid)
        except (TypeError, ValueError):
            pass


def _cid_of(state: dict[str, Any], env: dict[str, Any]) -> int | None:
    _register_metadata(state, env)
    rid = env.get("response_id")
    if rid is None and isinstance(env.get("response"), dict):
        rid = env["response"].get("id")
    cid = state["rid_to_cid"].get(rid)
    return cid if isinstance(cid, int) and 0 <= cid < state["n"] else None


def _batch_route(state: dict[str, Any], env: dict[str, Any]) -> None:
    """Update batch *state* from one parsed server envelope (pure; no I/O)."""
    kind = env.get("type", "")
    if kind in ("error", "response.failed"):
        cid = _cid_of(state, env)
        err = env.get("error") if kind == "error" else (env.get("response") or {}).get("error")
        if cid is None:
            # No response context -> session-level failure; abort the whole batch.
            state["session_error"] = _provider_error(err)
            return
        state["error"][cid] = _provider_error(err)
        state["finished"].add(cid)
        return
    cid = _cid_of(state, env)
    if cid is None:
        return
    if kind == "response.function_call_arguments.delta":
        d = env.get("delta")
        if isinstance(d, str):
            state["args"][cid].append(d)
    elif kind == "response.function_call_arguments.done":
        a = env.get("arguments")
        if isinstance(a, str):
            state["done_args"][cid] = a
    elif kind == "response.output_text.delta":
        d = env.get("delta")
        if isinstance(d, str):
            state["text"][cid].append(d)
    elif kind in ("response.done", "response.completed"):
        if state["done_args"][cid] is None:
            state["done_args"][cid] = _function_args_from_done(env)
        state["finished"].add(cid)


def _batch_chosen(state: dict[str, Any], cid: int) -> str:
    return state["done_args"][cid] or ("".join(state["args"][cid]) if state["args"][cid] else "") or "".join(state["text"][cid])


def _collect_batch(envelopes: list[dict[str, Any]], n: int) -> list[tuple[str | None, str | None]]:
    """Route a full envelope sequence and return [(chosen_args, error)] per cid.

    Pure helper used both by the live loop and by tests (synthetic frames)."""
    state = _new_batch_state(n)
    for env in envelopes:
        _batch_route(state, env)
        if state["session_error"]:
            break
    out: list[tuple[str | None, str | None]] = []
    for i in range(n):
        if state["session_error"] and i not in state["finished"]:
            out.append((None, state["session_error"]))
            continue
        if state["error"][i]:
            out.append((None, state["error"][i]))
            continue
        chosen = _batch_chosen(state, i)
        out.append((chosen or None, None))
    return out


def _response_create(req: dict[str, Any], cid: int) -> dict[str, Any]:
    from transcript_tail import JUDGE_EFFECTIVE_MAX_CHARS, cap_judge_message

    system = cap_judge_message(str(req.get("system") or ""), JUDGE_EFFECTIVE_MAX_CHARS)
    user = cap_judge_message(str(req.get("user") or ""), JUDGE_EFFECTIVE_MAX_CHARS - len(_QUESTION_PREFIX))
    tool = {
        "type": "function",
        "name": str(req.get("schema_name") or "result"),
        "description": "Return the structured result. Call exactly once with the complete object.",
        "parameters": req.get("schema") or {},
    }
    return {
        "type": "response.create",
        "response": {
            "conversation": "none",
            "output_modalities": ["text"],
            "instructions": system,
            "tools": [tool],
            "tool_choice": "required",
            **_realtime_reasoning_config(),
            "metadata": {"cid": str(cid)},
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": f"{_QUESTION_PREFIX}{user}"}],
                }
            ],
        },
    }


def _batch_once(
    chunk: list[dict[str, Any]], model: str, auth_path: str | os.PathLike[str] | None, timeout: float, force_refresh: bool
) -> dict[str, Any]:
    tokens = _fresh_tokens(auth_path, force=force_refresh)
    sock = _ws_connect(tokens, model, HANDSHAKE_TIMEOUT)
    state = _new_batch_state(len(chunk))
    try:
        _send_text(sock, {"type": "session.update", "session": {"type": "realtime", "output_modalities": ["text"]}})
        for cid, req in enumerate(chunk):
            _send_text(sock, _response_create(req, cid))
        deadline = time.monotonic() + timeout
        while len(state["finished"]) < len(chunk) and not state["session_error"]:
            if time.monotonic() >= deadline:
                break
            opcode, payload = _read_message(sock)
            if opcode == 0x8:
                break
            if opcode == 0x9:
                sock.sendall(_encode_frame(payload, opcode=0xA))
                continue
            if opcode not in (0x0, 0x1, 0x2):
                continue
            try:
                env = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            _batch_route(state, env)
        return state
    finally:
        try:
            sock.sendall(_encode_frame(b"", opcode=0x8))
            sock.close()
        except OSError:
            pass


def ask_structured_batch(
    requests: list[dict[str, Any]],
    *,
    model: str = MODEL,
    auth_path: str | os.PathLike[str] | None = None,
    timeout: float = READ_TIMEOUT,
) -> list[dict[str, Any] | JudgeError]:
    """Ask many structured questions concurrently over ONE WebSocket.

    requests: [{system, user, schema, schema_name}]. Returns a list aligned to the
    input: each entry is the parsed object, or a JudgeError for that slot (never
    raises for a single bad slot, so one failed judge can't poison the others). A
    handshake auth rejection refreshes the token and retries once, like
    ask_structured."""
    results: list[dict[str, Any] | JudgeError] = [JudgeError("no result") for _ in requests]
    if not requests:
        return []
    for start in range(0, len(requests), BATCH_MAX_INFLIGHT):
        chunk = requests[start : start + BATCH_MAX_INFLIGHT]
        try:
            state = _batch_once(chunk, model, auth_path, timeout, force_refresh=False)
        except JudgeError as exc:
            if "handshake rejected" in str(exc):
                try:
                    state = _batch_once(chunk, model, auth_path, timeout, force_refresh=True)
                except JudgeError as exc2:
                    for j in range(len(chunk)):
                        results[start + j] = exc2
                    continue
            else:
                for j in range(len(chunk)):
                    results[start + j] = exc
                continue
        for j in range(len(chunk)):
            if state["session_error"] and j not in state["finished"]:
                results[start + j] = JudgeError(state["session_error"])
                continue
            if state["error"][j]:
                results[start + j] = JudgeError(state["error"][j])
                continue
            chosen = _batch_chosen(state, j)
            if not chosen.strip():
                results[start + j] = JudgeError("realtime stream produced no structured output")
                continue
            try:
                obj = json.loads(chosen)
            except json.JSONDecodeError as exc:
                results[start + j] = JudgeError(f"output is not valid json: {exc}")
                continue
            results[start + j] = obj if isinstance(obj, dict) else JudgeError("output is not a json object")
    return results


if __name__ == "__main__":  # tiny live smoke test against gpt-realtime-2
    import sys

    schema = {
        "type": "object",
        "properties": {
            "verdict": {"type": "integer", "enum": [0, 1]},
            "reason": {"type": "string"},
        },
        "required": ["verdict", "reason"],
        "additionalProperties": False,
    }
    try:
        out = ask_structured(
            "You are a strict validator. Return verdict 1 only if the user's statement is true, else 0, with a one-line reason.",
            "Statement: 1 + 1 = 2.",
            schema,
            schema_name="verdict",
        )
        print(f"OK model={MODEL}", json.dumps(out))
        sys.exit(0 if out.get("verdict") == 1 else 2)
    except JudgeError as e:
        print("JudgeError:", e, file=sys.stderr)
        sys.exit(1)
