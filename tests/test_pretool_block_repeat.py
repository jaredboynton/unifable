#!/usr/bin/env python3
"""Regression: turnless repeated same-signature blocks must not emit empty stderr.

Claude Code sends no ``turn_id`` (it is the signal used to detect Codex in
``hook_output.detect_host``), so the block epoch falls back to the stable
task/session scope and the per-signature count never resets across the task.
Before the fix, every repeat after the first returned exit 2 with empty stderr,
which Claude Code renders as a bare ``hook error: No stderr output`` blank wall
that gives the model nothing to act on and drives blind retries (observed in the
hermetic benchmark transcripts). The fix emits a compact one-line pointer on
turnless repeats while preserving the silent parallel dedup for turn-scoped
hosts (Codex).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "gate"
sys.path.insert(0, str(GATE))

from pretool_block import (  # noqa: E402
    emit_pretool_block,
    format_bash_research_block,
)


def _emit(payload: dict, capsys) -> tuple[int, str]:
    rc = emit_pretool_block(
        payload,
        kind="bash",
        detail="grep",
        full_message=format_bash_research_block(
            "grep is not in the Bash research whitelist", payload["session_id"]
        ),
    )
    return rc, capsys.readouterr().err


def test_turnless_repeat_emits_compact_pointer_not_empty(tmp_path, capsys):
    os.environ["UNIFABLE_DATA"] = str(tmp_path)
    payload = {"session_id": "turnless-repeat", "cwd": str(tmp_path)}  # no turn_id

    rc1, err1 = _emit(payload, capsys)
    assert rc1 == 2
    assert "Unlock:" in err1  # first sighting carries the full message

    rc2, err2 = _emit(payload, capsys)
    assert rc2 == 2
    # The bug: err2 used to be "" -> Claude "hook error: No stderr output".
    assert err2.strip() != "", "turnless repeat must not emit empty stderr"
    assert "grep is not in the Bash research whitelist" in err2  # names the block
    assert "earlier gate message" in err2  # points back to the full message
    assert "Unlock:" not in err2  # compact: unlock footer not repeated

    rc3, err3 = _emit(payload, capsys)
    assert rc3 == 2
    assert err3.strip() != ""  # every repeat keeps emitting, never empty


def test_turn_scoped_repeat_stays_silent(tmp_path, capsys):
    """Codex (turn_id present) keeps silent parallel dedup -- regression guard."""
    os.environ["UNIFABLE_DATA"] = str(tmp_path)
    payload = {"session_id": "turn-scoped", "cwd": str(tmp_path), "turn_id": "t-1"}

    rc1, err1 = _emit(payload, capsys)
    assert rc1 == 2
    assert "Unlock:" in err1

    rc2, err2 = _emit(payload, capsys)
    assert rc2 == 2
    assert err2.strip() == ""  # turn-scoped repeat stays silent (no blank-wall risk)
