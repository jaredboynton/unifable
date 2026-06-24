#!/usr/bin/env python3
"""retention_window: sticky chunked truncation that keeps a byte-identical,
append-only prefix across calls (so the transcript caches) instead of sliding by
one unit per appended char (which busts the prompt cache every turn).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

from transcript_tail import _sticky_start, retention_window  # noqa: E402


def test_under_budget_returned_whole():
    assert retention_window("hello", 100) == "hello"


def test_window_is_suffix_and_bounded():
    text = "A" * 1000
    out = retention_window(text, 100, retention_ratio=0.8)
    assert text.endswith(out)
    assert len(out) <= 100


def test_prefix_stable_across_appends_within_chunk():
    # ratio 0.8, budget 100 -> drop_chunk = 20. Appending a few chars must NOT shift
    # the retained window start, so the new output is the old output + appended tail.
    base = "X" * 130
    out1 = retention_window(base, 100, 0.8)
    out2 = retention_window(base + "Y" * 5, 100, 0.8)
    assert out2 == out1 + "YYYYY"


def test_sticky_start_jumps_by_chunk_on_boundary():
    # budget 100, ratio 0.8 -> drop_chunk 20
    assert _sticky_start(120, 100, 0.8) == 20   # overflow 20 -> 1 chunk
    assert _sticky_start(121, 100, 0.8) == 40   # overflow 21 -> 2 chunks (jump)
    assert _sticky_start(139, 100, 0.8) == 40   # still within 2nd chunk -> stable
    assert _sticky_start(100, 100, 0.8) == 0    # at budget -> no drop


def test_ratio_one_is_plain_last_n():
    assert _sticky_start(120, 100, 1.0) == 20
    assert retention_window("Z" * 120, 100, 1.0) == "Z" * 100


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
