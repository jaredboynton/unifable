#!/usr/bin/env python3
"""Pure session helpers for the Realtime structured judge (no I/O).

These functions implement the parts of the structured/batch ask that are pure
state and classification: provider-error formatting, function-call argument
extraction, the reask-on-malformed-output classifiers, the reasoning config, and
the concurrent-batch frame router. They take no socket and touch no auth, so they
are unit-testable from synthetic frames (tests/test_codex_judge_batch.py,
tests/test_codex_judge_reask.py drive them through the codex_judge re-exports).

The connect/read/auth side lives in ``realtime_ws``; the gate adapter
``scripts/gate/codex_judge.py`` composes both and owns the 256k message capping
(via ``transcript_tail``) plus the env-driven constants (MODEL, timeouts, reask).
"""

from __future__ import annotations

import json
from typing import Any

# Error-message fragments that mean the failure is operational (not bad output)
# and must NOT be reasked -- re-issuing cannot help and a daemon reask on a dead
# socket is wasted work (the caller falls back to a direct call instead).
_DIRECT_INELIGIBLE = ("handshake rejected",)  # has its own force-refresh retry
_DAEMON_INELIGIBLE = (
    "judge websocket closed",
    "judge websocket unavailable",
    "timed out",  # RuntimeError/TimeoutError from submit -- let caller fall back
)


# --- reask classifiers (mirror explore submit-phase reask) --------------------


def reask_eligible(err_msg: str, ineligible: tuple[str, ...]) -> bool:
    """True if a failure message looks like malformed output (worth one reask).

    Operational failures whose fragments appear in ``ineligible`` return False so
    the caller skips the reask and surfaces the error for fail-open fallback."""
    low = (err_msg or "").lower()
    return not any(frag.lower() in low for frag in ineligible)


def reask_reason_from_text(text: str) -> str | None:
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


def augment_user_text(user_text: str, reason: str) -> str:
    """Append the failure reason to the user text so the reask sees it (explore's
    ``PREVIOUS SUBMIT FAILED`` pattern). Left uncapped; the sender caps per the
    256k field limit before transmit."""
    return (
        f"{user_text}\n\nPREVIOUS JUDGE CALL FAILED: {reason}\n"
        "Return the complete valid object again, calling the tool exactly once."
    )


def reasoning_config(effort: str | None, *, model: str, default_effort: str) -> dict[str, Any]:
    """Realtime ``reasoning.effort`` for gpt-realtime-2 judge/breaker calls.

    Returns an empty dict (no ``reasoning`` key) when reasoning is unsupported or
    explicitly disabled: gpt-realtime-MINI rejects the ``reasoning`` option with
    'Unsupported option for this model', and callers may pass effort 'none'/'off'
    to opt out. Spreading an empty dict into the response leaves it absent."""
    e = (effort or default_effort).strip().lower()
    if e in ("none", "off", "no", "disabled") or "mini" in model.lower():
        return {}
    return {"reasoning": {"effort": e or "low"}}


# --- response parsing ---------------------------------------------------------


def provider_error(err: Any) -> str:
    if not isinstance(err, dict):
        return "provider error (no detail)"
    code = err.get("code") or err.get("type") or "unknown"
    return f"{code}: {err.get('message') or '(no detail)'}"


def function_args_from_done(env: dict[str, Any]) -> str | None:
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


def new_batch_state(n: int) -> dict[str, Any]:
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


def batch_route(state: dict[str, Any], env: dict[str, Any]) -> None:
    """Update batch *state* from one parsed server envelope (pure; no I/O)."""
    kind = env.get("type", "")
    if kind in ("error", "response.failed"):
        cid = _cid_of(state, env)
        err = env.get("error") if kind == "error" else (env.get("response") or {}).get("error")
        if cid is None:
            # No response context -> session-level failure; abort the whole batch.
            state["session_error"] = provider_error(err)
            return
        state["error"][cid] = provider_error(err)
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
            state["done_args"][cid] = function_args_from_done(env)
        state["finished"].add(cid)


def batch_chosen(state: dict[str, Any], cid: int) -> str:
    return state["done_args"][cid] or ("".join(state["args"][cid]) if state["args"][cid] else "") or "".join(
        state["text"][cid]
    )


def collect_batch(envelopes: list[dict[str, Any]], n: int) -> list[tuple[str | None, str | None]]:
    """Route a full envelope sequence and return [(chosen_args, error)] per cid.

    Pure helper used both by the live loop and by tests (synthetic frames)."""
    state = new_batch_state(n)
    for env in envelopes:
        batch_route(state, env)
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
        chosen = batch_chosen(state, i)
        out.append((chosen or None, None))
    return out
