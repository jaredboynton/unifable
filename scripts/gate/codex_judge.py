#!/usr/bin/env python3
"""gpt-realtime-2 structured-output client -- gate adapter over the canonical transport.

The host-agnostic Realtime transport now lives in ``unifable_runtime.transport``:
``realtime_ws`` (RFC 6455 client, frame encode/decode, OAuth token lifecycle,
connection lifecycle) and ``realtime_session`` (pure response routing, reask
classifiers, reasoning config, batch frame router). This module composes both
behind the stable public API and owns the gate-specific orchestration: the 256k
message cap (``transcript_tail``), the env-driven constants (MODEL, timeouts,
reask), token-usage recording (``judge_usage``), and the ``ask_structured`` /
``ask_structured_batch`` control flow with its handshake-refresh + reask retries.

It speaks the OpenAI Realtime WebSocket protocol to
``wss://api.openai.com/v1/realtime?model=gpt-realtime-2``, authenticated with the
Codex ChatGPT OAuth bearer in ``~/.codex/auth.json`` -- no platform
OPENAI_API_KEY, no TLS fingerprint.

Structured output rides a single function tool with ``tool_choice: "required"``:
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
    ask_structured_batch(requests, *, model=MODEL, auth_path=None, timeout=...) -> list
    render_structured_request(system, user, schema, *, schema_name="result") -> dict
    JudgeError -- raised on any auth/transport/protocol failure.

Stdlib only: socket + ssl (WebSocket), urllib (OAuth refresh), base64/struct/json.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Resolve unifable_runtime from the repo checkout or the synced stable runtime.
# scripts/gate/codex_judge.py -> parents[2] is the repo root (checkout) or
# ~/.unifable/current (synced), and unifable_runtime sits alongside scripts/ in
# both layouts. The hook/CLI launchers also export it on PYTHONPATH, so this is a
# belt-and-suspenders fallback for direct imports (tests put only scripts/gate on
# the path).
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from unifable_runtime.transport import realtime_session as _rs  # noqa: E402
from unifable_runtime.transport import realtime_ws as _ws  # noqa: E402

# --- Realtime + codex OAuth constants (re-exported from the transport) --------
REALTIME_HOST = _ws.REALTIME_HOST
REALTIME_PATH = _ws.REALTIME_PATH
OAUTH_TOKEN_URL = _ws.OAUTH_TOKEN_URL
OAUTH_CLIENT_ID = _ws.OAUTH_CLIENT_ID
OAUTH_SCOPE = _ws.OAUTH_SCOPE
ORIGINATOR = _ws.ORIGINATOR

JudgeError = _ws.JudgeError

# Transport-owned WebSocket + auth helpers. Re-exported under their historical
# private names so call sites (realtime_daemon.py) and tests that monkeypatch
# ``cj._ws_connect`` / ``cj._fresh_tokens`` / build frames with ``cj._encode_frame``
# keep working unchanged.
_fresh_tokens = _ws._fresh_tokens
_encode_frame = _ws._encode_frame
_read_frame = _ws._read_frame
_read_message = _ws._read_message
_send_text = _ws._send_text
_ws_connect = _ws._ws_connect
_read_exactly = _ws._read_exactly
_refresh = _ws._refresh
_jwt_exp_unix = _ws._jwt_exp_unix
_auth_path = _ws._auth_path
_atomic_write = _ws._atomic_write

# Session-owned pure helpers, re-exported under their historical names.
_DIRECT_INELIGIBLE = _rs._DIRECT_INELIGIBLE
_DAEMON_INELIGIBLE = _rs._DAEMON_INELIGIBLE
_reask_eligible = _rs.reask_eligible
_reask_reason_from_text = _rs.reask_reason_from_text
_augment_user_text = _rs.augment_user_text
_provider_error = _rs.provider_error
_function_args_from_done = _rs.function_args_from_done
_new_batch_state = _rs.new_batch_state
_register_metadata = _rs._register_metadata
_cid_of = _rs._cid_of
_batch_route = _rs.batch_route
_batch_chosen = _rs.batch_chosen
_collect_batch = _rs.collect_batch

# gpt-realtime-2 over the Realtime API, authenticated with the Codex ChatGPT
# OAuth bearer (tokens.access_token in ~/.codex/auth.json) -- the same path
# cse-tools uses, no platform API key. Override with UNIFABLE_JUDGE_MODEL.
MODEL = os.environ.get("UNIFABLE_JUDGE_MODEL", "gpt-realtime-2")
REASONING_EFFORT = (os.environ.get("UNIFABLE_JUDGE_REASONING_EFFORT") or "low").strip() or "low"

_QUESTION_PREFIX = "QUESTION: "

# Timeouts live in the transport (reconciled with the host Stop-hook budget; see
# tests/test_stop_timeout_budget.py). Re-exported so the timeout-budget tests and
# callers continue to read codex_judge.HANDSHAKE_TIMEOUT / READ_TIMEOUT.
HANDSHAKE_TIMEOUT = _ws.HANDSHAKE_TIMEOUT
READ_TIMEOUT = _ws.READ_TIMEOUT
REFRESH_TIMEOUT = _ws.REFRESH_TIMEOUT
_HANDSHAKE_TIMEOUT = HANDSHAKE_TIMEOUT  # back-compat alias

# Ceiling on out-of-band responses launched on one socket per batch; larger
# batches are chunked so no socket exceeds it. Set from measurement, not a guess.
# Re-validated live 2026-06-25 (skills/unitrace/docs/benchmarks/realtime-concurrency.md):
# gpt-realtime-2 was 100% clean at M=128 and dropped ~11% at M=224; gpt-realtime-mini
# was clean at BOTH 128 and 224. The Realtime API never hard-caps or rate-errors
# these; the failure is SILENT -- a completed response with empty structured output
# (the model skips the required tool call under load), which ask_structured_batch
# maps to a per-slot JudgeError. 128 keeps wide margin below the full model's ~224
# degradation onset (mini has more headroom). Probabilistic with high variance,
# hence the margin. See docs: https://developers.openai.com/api/docs/guides/realtime-conversations
BATCH_MAX_INFLIGHT = int(os.environ.get("UNIFABLE_JUDGE_BATCH_MAX") or 128)

# --- Reask on malformed output ------------------------------------------------
# gpt-realtime-2 occasionally emits a malformed structured response under load:
# empty function-call arguments (the model skips the required tool call), a
# wrong-shape object, invalid JSON, or a per-response ``response.failed``. These
# are not transport/auth failures -- the response completed, the content is just
# bad -- so feeding the reason back and re-issuing the request almost always
# recovers on the next try. This mirrors the unitrace skill submit-phase reask
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
REASK_FLOOR = _ws._env_float("UNIFABLE_JUDGE_REASK_FLOOR", 10.0)


def _realtime_reasoning_config(effort: str | None = None) -> dict[str, Any]:
    """Realtime ``reasoning.effort`` for gpt-realtime-2 judge/breaker calls.

    Thin gate-side wrapper binding the env-configured MODEL + default effort to
    the pure ``realtime_session.reasoning_config`` (kept here so MODEL changes via
    UNIFABLE_JUDGE_MODEL are honored at call time)."""
    return _rs.reasoning_config(effort, model=MODEL, default_effort=REASONING_EFFORT)


# ---------------------------------------------------------------------------
# Realtime structured ask (function tool + tool_choice=required)
# ---------------------------------------------------------------------------


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
    # Hermetic test knob: when set, the judge is deterministically unreachable so a
    # hook's breaker/director/Stop judging fails open regardless of whether the dev
    # machine has live Realtime credentials. Checked here at the real network
    # boundary (not in judge_transport) so an in-process test that patches
    # ``codex_judge.ask_structured`` cleanly replaces this path. Production never
    # sets it; the gate stays always-on.
    if os.environ.get("UNIFABLE_JUDGE_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on"):
        raise JudgeError("judge offline (UNIFABLE_JUDGE_OFFLINE)")
    from transcript_tail import JUDGE_EFFECTIVE_MAX_CHARS

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
# Routing/state lives in realtime_session; the live socket loop stays here.
# ---------------------------------------------------------------------------


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
            **_realtime_reasoning_config(req.get("reasoning_effort")),
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
    if os.environ.get("UNIFABLE_JUDGE_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on"):
        return [JudgeError("judge offline (UNIFABLE_JUDGE_OFFLINE)") for _ in requests]
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
