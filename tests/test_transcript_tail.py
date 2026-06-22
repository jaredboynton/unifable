#!/usr/bin/env python3
"""Regression: the goal-judge transcript tail must never exceed the model's
input-char limit, even when tiktoken is absent.

Live failure that motivated this: a long session's Stop hook reported
`[G002] goal judge unavailable: string_above_max_length ... got a string with
length 497049` (limit 256000). Root cause: with no tiktoken, tail_tokens kept the
last N *whitespace-delimited spans*; a transcript dense with JSON/code/IDs has
very long spans, so N spans >> N tokens worth of chars and the tail overflowed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "gate"))

from transcript_tail import (  # noqa: E402
    JUDGE_MAX_MESSAGE_CHARS,
    JUDGE_TRANSCRIPT_CHAR_BUDGET,
    MAX_CHARS_PER_TOKEN,
    TRANSCRIPT_TOKEN_BUDGET,
    cap_judge_message,
    tail_tokens,
)


def test_dense_low_whitespace_text_is_char_bounded():
    """A blob with very long \\S+ spans (the exact overflow case) is clamped to
    the char ceiling, not returned whole."""
    dense = ('{"x":"' + "A" * 5000 + '"}') * 200  # ~1M chars, ~200 whitespace spans
    out = tail_tokens(dense, max_tokens=TRANSCRIPT_TOKEN_BUDGET)
    cap = min(TRANSCRIPT_TOKEN_BUDGET * MAX_CHARS_PER_TOKEN, JUDGE_TRANSCRIPT_CHAR_BUDGET)
    assert len(out) <= cap
    assert len(out) < JUDGE_MAX_MESSAGE_CHARS  # the model input-char limit that was being blown
    assert dense.endswith(out)  # it is a tail, preserving the most recent text


def test_cap_judge_message_never_exceeds_limit():
    big = "B" * 400_000
    out = cap_judge_message(big)
    assert len(out) <= JUDGE_MAX_MESSAGE_CHARS
    assert "B" in out


def test_short_text_returned_whole():
    assert tail_tokens("hello world", max_tokens=TRANSCRIPT_TOKEN_BUDGET) == "hello world"


def test_zero_budget_is_empty():
    assert tail_tokens("anything", max_tokens=0) == ""


def test_custom_budget_char_cap():
    big = "x" * 100_000
    out = tail_tokens(big, max_tokens=1_000)
    assert len(out) == 1_000 * MAX_CHARS_PER_TOKEN


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
