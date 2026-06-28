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

import json  # noqa: E402

from transcript_tail import (  # noqa: E402
    JUDGE_MAX_MESSAGE_CHARS,
    JUDGE_TRANSCRIPT_CHAR_BUDGET,
    MAX_CHARS_PER_TOKEN,
    TRANSCRIPT_TOKEN_BUDGET,
    cap_judge_message,
    stripped_transcript,
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


def test_codex_response_item_message_renders():
    """Codex `response_item` records nest text under top-level `payload`; the judge
    renderer must surface it instead of `[no textual content extracted]`."""
    line = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "codex fixture proof"}],
            },
        }
    )
    out = stripped_transcript(line)
    assert "codex fixture proof" in out
    assert "[no textual content extracted]" not in out


def test_codex_event_msg_message_field_renders():
    line = json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "event text here"}})
    out = stripped_transcript(line)
    assert "event text here" in out
    assert "[no textual content extracted]" not in out


def test_codex_tool_result_ok_content_renders():
    line = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "result": {"Ok": {"content": [{"type": "text", "text": "tool output line"}]}},
            },
        }
    )
    out = stripped_transcript(line)
    assert "tool output line" in out
    assert "[no textual content extracted]" not in out


def test_claude_shaped_record_unaffected_by_payload_fallback():
    line = json.dumps(
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "claude shaped"}]}}
    )
    out = stripped_transcript(line)
    assert "claude shaped" in out


def test_repl_bash_tooluseresult_stdout_surfaced():
    """REPL-only sessions leave inline tool_result.content empty and put stdout in
    the record-level toolUseResult; the judge renderer must surface it, not just
    the bare [tool_result] placeholder."""
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "repl_x", "content": "", "is_error": False}],
            },
            "toolUseResult": {
                "stdout": "SECRET SCANS: running\nSECRET SCAN OK: gitleaks\n[pre-commit] all checks passed\nOK",
                "stderr": "",
                "interrupted": False,
            },
        }
    )
    out = stripped_transcript(line)
    assert "SECRET SCAN OK" in out
    assert "all checks passed" in out
    assert "[tool_result]" in out  # framing preserved


def test_repl_read_tooluseresult_file_content_surfaced():
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "repl_r", "content": "", "is_error": False}],
            },
            "toolUseResult": {
                "type": "text",
                "file": {"filePath": "/repo/.gitignore", "content": "node_modules/\n.env\nreference/\n", "numLines": 3},
            },
        }
    )
    out = stripped_transcript(line)
    assert "node_modules/" in out
    assert "reference/" in out


def test_repl_webfetch_tooluseresult_result_surfaced():
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "repl_w", "content": "", "is_error": False}],
            },
            "toolUseResult": {"url": "https://x.io", "code": 200, "result": "FETCHED BODY MARKER abc"},
        }
    )
    out = stripped_transcript(line)
    assert "FETCHED BODY MARKER abc" in out


def test_inline_tool_result_unaffected_by_tooluseresult_fallback():
    """A normal inline tool_result body still renders [tool_result] + its content,
    and the fallback does not append a duplicate body."""
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "ran probe -> 0 ok"}],
            },
            "toolUseResult": {"stdout": "SHOULD NOT APPEAR TWICE"},
        }
    )
    out = stripped_transcript(line)
    assert "ran probe -> 0 ok" in out
    assert "SHOULD NOT APPEAR TWICE" not in out


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
