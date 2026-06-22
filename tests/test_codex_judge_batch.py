#!/usr/bin/env python3
"""Concurrent batch routing for the Realtime judge (scripts/gate/codex_judge.py).

Drives the pure frame-routing with synthetic, interleaved out-of-band response
frames (no network) to prove that N concurrent responses on one socket are
correlated back to the right request by response_id, and that per-slot failures
do not poison the others."""

from __future__ import annotations

import os
import sys

REPO = os.path.join(os.path.dirname(__file__), "..", "scripts", "gate")
sys.path.insert(0, REPO)

from codex_judge import (  # noqa: E402
    _batch_chosen,
    _batch_route,
    _collect_batch,
    _new_batch_state,
    _response_create,
)


def _created(rid, cid):
    return {"type": "response.created", "response": {"id": rid, "metadata": {"cid": str(cid)}}}


def _args_done(rid, payload):
    return {"type": "response.function_call_arguments.done", "response_id": rid, "arguments": payload}


def _done(rid):
    return {"type": "response.done", "response": {"id": rid}}


def test_three_concurrent_responses_correlated_by_response_id():
    # Interleave frames for three responses; created order != completion order.
    envs = [
        _created("rA", 0),
        _created("rB", 1),
        _created("rC", 2),
        _args_done("rC", '{"verdict": 1, "v": "C"}'),
        _args_done("rA", '{"verdict": 0, "v": "A"}'),
        _done("rC"),
        _args_done("rB", '{"verdict": 1, "v": "B"}'),
        _done("rA"),
        _done("rB"),
    ]
    results = _collect_batch(envs, 3)
    assert results[0] == ('{"verdict": 0, "v": "A"}', None)
    assert results[1] == ('{"verdict": 1, "v": "B"}', None)
    assert results[2] == ('{"verdict": 1, "v": "C"}', None)


def test_per_slot_error_does_not_poison_others():
    envs = [
        _created("rA", 0),
        _created("rB", 1),
        {"type": "response.failed", "response": {"id": "rB", "error": {"message": "boom"}}},
        _args_done("rA", '{"ok": true}'),
        _done("rA"),
    ]
    results = _collect_batch(envs, 2)
    assert results[0] == ('{"ok": true}', None)
    chosen, err = results[1]
    assert chosen is None and err and "boom" in err


def test_session_error_fails_unfinished_slots():
    envs = [
        _created("rA", 0),
        _created("rB", 1),
        _args_done("rA", '{"ok": true}'),
        _done("rA"),
        {"type": "error", "error": {"message": "session died"}},
    ]
    results = _collect_batch(envs, 2)
    assert results[0] == ('{"ok": true}', None)
    chosen, err = results[1]
    assert chosen is None and err and "session died" in err


def test_delta_accumulation_when_no_done_arguments():
    state = _new_batch_state(1)
    for env in [
        _created("r0", 0),
        {"type": "response.function_call_arguments.delta", "response_id": "r0", "delta": '{"a":'},
        {"type": "response.function_call_arguments.delta", "response_id": "r0", "delta": " 1}"},
        _done("r0"),
    ]:
        _batch_route(state, env)
    assert _batch_chosen(state, 0) == '{"a": 1}'


def test_response_create_payload_is_out_of_band_with_tool():
    req = {"system": "be strict", "user": "1+1=2?", "schema": {"type": "object"}, "schema_name": "verdict"}
    payload = _response_create(req, 3)
    resp = payload["response"]
    assert payload["type"] == "response.create"
    assert resp["conversation"] == "none"          # out of band -> parallelizable
    assert resp["tool_choice"] == "required"
    assert resp["tools"][0]["name"] == "verdict"
    assert resp["metadata"] == {"cid": "3"}
    assert resp["input"][0]["content"][0]["text"].endswith("1+1=2?")
