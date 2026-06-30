#!/usr/bin/env python3
"""PreToolUse _block must NOT double-print breaker_notify to stderr.

Regression for the removed `print(breaker_notify, file=sys.stderr)` in _block:
standing breaker state (lift/disarm/verify prose) must not leak onto the block
path. The block `message` is the single channel for block guidance on stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))
sys.path.insert(0, str(REPO / "hooks"))

import pre_tool_use as ptu  # noqa: E402


def _payload(tmp_path):
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tmp_path / "x.py")},
        "session_id": "dblprint",
        "cwd": str(tmp_path),
    }


def test_block_does_not_print_breaker_notify_to_stderr(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    breaker_notify = "SECRET-LIFT-PROSE-MUST-NOT-LEAK"
    rc = ptu._block(
        _payload(tmp_path),
        kind="breaker",
        detail="edit",
        message="Ground the claim before mutating: read foo.py:10 and cite the constant.",
        breaker_notify=breaker_notify,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert breaker_notify not in err
    # The block message itself still reaches stderr (the single channel).
    assert "Ground the claim" in err


def test_block_emits_message_even_when_notify_overlaps(monkeypatch, tmp_path, capsys):
    # Before the fix, is_redundant_with_notify suppressed the block message when
    # it overlapped the notify; combined with the notify no longer printing, that
    # would emit an empty block. The block message must always be shown now.
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
    monkeypatch.setenv("UNIFABLE_GRADE", "STANDARD")
    shared = "Read foo.py:10 and cite the constant."
    rc = ptu._block(
        _payload(tmp_path),
        kind="breaker",
        detail="edit",
        message=shared,
        breaker_notify=shared,
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "Read foo.py:10" in err


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
