#!/usr/bin/env python3
"""Reask-on-malformed-structured-output for the realtime judge (codex_judge.py).

Mirrors the explore skill's submit-phase reask: one bounded retry when the model
returns malformed structured output (empty, invalid JSON, wrong shape, or a
per-response failure), feeding the failure reason back into the next prompt. The
reask must preserve the existing transport: one shared deadline, the
handshake-refresh retry, and fail-open on operational errors.

These tests monkeypatch the single-attempt worker (_ask_once) so no network or
auth is touched; they exercise the ask_structured control flow directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import codex_judge as cj  # noqa: E402

_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


# --- pure classifiers ---------------------------------------------------------


def test_reask_reason_classifies_output() -> None:
    assert cj._reask_reason_from_text('{"ok": true}') is None
    assert cj._reask_reason_from_text("") is not None
    assert cj._reask_reason_from_text("   ") is not None
    assert cj._reask_reason_from_text("not json at all") is not None
    assert cj._reask_reason_from_text("[1, 2, 3]") is not None  # array, not object


def test_reask_eligible_skips_operational_failures() -> None:
    # Operational handshake failure: NOT worth a reask (has its own retry).
    assert cj._reask_eligible("handshake rejected (401)", cj._DIRECT_INELIGIBLE) is False
    # Malformed-output style message: eligible for one reask.
    assert cj._reask_eligible("output is not valid json", cj._DIRECT_INELIGIBLE) is True


def test_augment_user_text_appends_reason() -> None:
    out = cj._augment_user_text("original question", "output is not a json object")
    assert "original question" in out
    assert "output is not a json object" in out
    assert "PREVIOUS JUDGE CALL FAILED" in out


# --- ask_structured control flow ----------------------------------------------


def _patch_ask_once(monkeypatch, side_effects):
    """Replace _ask_once with a scripted sequence; record (user_text, force_refresh)."""
    calls: list[dict] = []
    seq = list(side_effects)

    def fake(auth_path, model, su, q, rc, user_text, user_cap, deadline, on_usage, *, force_refresh=False):
        calls.append({"user_text": user_text, "force_refresh": force_refresh})
        eff = seq[min(len(calls) - 1, len(seq) - 1)]
        if isinstance(eff, Exception):
            raise eff
        return eff

    monkeypatch.setattr(cj, "_ask_once", fake)
    return calls


def test_one_shot_recovery_from_bad_json(monkeypatch) -> None:
    calls = _patch_ask_once(monkeypatch, ["this is not json", '{"ok": true}'])
    result = cj.ask_structured("sys", "ask the question", _SCHEMA, timeout=60)
    assert result == {"ok": True}
    assert len(calls) == 2  # one reask
    # The reask prompt carries the failure reason.
    assert "PREVIOUS JUDGE CALL FAILED" in calls[1]["user_text"]


def test_recovery_when_first_attempt_raises_empty(monkeypatch) -> None:
    # Empty structured output surfaces as a JudgeError from _ask_once; it is
    # malformed-eligible, so the loop reasks rather than failing.
    calls = _patch_ask_once(
        monkeypatch,
        [cj.JudgeError("realtime stream produced no structured output"), '{"ok": false}'],
    )
    result = cj.ask_structured("sys", "q", _SCHEMA, timeout=60)
    assert result == {"ok": False}
    assert len(calls) == 2


def test_exhausted_reask_raises(monkeypatch) -> None:
    calls = _patch_ask_once(monkeypatch, ["bad", "still bad"])
    with pytest.raises(cj.JudgeError):
        cj.ask_structured("sys", "q", _SCHEMA, timeout=60)
    assert len(calls) == 2  # original + exactly one reask, then give up


def test_reask_disabled_fails_on_first_malformed(monkeypatch) -> None:
    calls = _patch_ask_once(monkeypatch, ["bad", '{"ok": true}'])
    with pytest.raises(cj.JudgeError):
        cj.ask_structured("sys", "q", _SCHEMA, timeout=60, reask=False)
    assert len(calls) == 1  # no reask attempted


def test_handshake_rejection_force_refreshes_not_reasks(monkeypatch) -> None:
    # A handshake rejection is operational: it triggers the force-refresh retry,
    # NOT the malformed reask. The retry must set force_refresh=True.
    calls = _patch_ask_once(
        monkeypatch,
        [cj.JudgeError("handshake rejected (401 Unauthorized)"), '{"ok": true}'],
    )
    result = cj.ask_structured("sys", "q", _SCHEMA, timeout=60)
    assert result == {"ok": True}
    assert len(calls) == 2
    assert calls[1]["force_refresh"] is True
    # The force-refresh retry reuses the ORIGINAL text (no failure-reason augmentation).
    assert "PREVIOUS JUDGE CALL FAILED" not in calls[1]["user_text"]


def test_handshake_refresh_then_malformed_text_reasks(monkeypatch) -> None:
    # Composition: a handshake rejection forces a refresh retry; if THAT retry
    # returns malformed text, the malformed-output reask still fires once. So the
    # two recovery paths interact: force-refresh (operational) then one reask
    # (content) under the same shared deadline.
    calls = _patch_ask_once(
        monkeypatch,
        [cj.JudgeError("handshake rejected"), "still not json", '{"ok": true}'],
    )
    result = cj.ask_structured("sys", "q", _SCHEMA, timeout=60)
    assert result == {"ok": True}
    assert len(calls) == 3
    assert calls[1]["force_refresh"] is True  # the handshake retry
    assert "PREVIOUS JUDGE CALL FAILED" in calls[2]["user_text"]  # the content reask


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
