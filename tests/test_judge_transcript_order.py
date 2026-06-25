#!/usr/bin/env python3
"""judge_transcript ordering: the big append-only host transcript is the stable
cacheable prefix and comes FIRST; small volatile records (breaker events, spec
board, fresh tool output) are reserved at the END.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import groundedness as gb  # noqa: E402
from breaker_state import append_event, default_breaker  # noqa: E402


def test_transcript_precedes_volatile_tail(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda input_data, max_tokens=None: "HOST-TRANSCRIPT-BODY")
    monkeypatch.setattr("breaker_runtime._spec_board_block", lambda input_data: "")

    st = default_breaker()
    append_event(st, "ARM", claim="c", steering="s")
    out = gb.judge_transcript({}, st["events"], fresh_tool="FRESH-OUTPUT")

    i_host = out.index("HOST-TRANSCRIPT-BODY")
    i_event = out.index("unifable_breaker")
    i_fresh = out.index("FRESH-OUTPUT")
    assert i_host < i_event < i_fresh


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
