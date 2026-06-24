#!/usr/bin/env python3
"""judge_usage: parse gpt-realtime-2 response.done usage and accumulate cache stats.

The cache rearchitecture is only justifiable if we can measure it; these lock the
parser (incl. cached_tokens) and the per-session accumulator behavior.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

from judge_usage import parse_usage, record_usage  # noqa: E402


def test_parse_usage_realtime_response_shape():
    env = {
        "type": "response.done",
        "response": {
            "usage": {
                "total_tokens": 253,
                "input_tokens": 132,
                "output_tokens": 121,
                "input_token_details": {"text_tokens": 119, "cached_tokens": 64},
            }
        },
    }
    assert parse_usage(env) == {
        "input_tokens": 132,
        "output_tokens": 121,
        "cached_tokens": 64,
        "total_tokens": 253,
    }


def test_parse_usage_top_level_fallback():
    env = {
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "input_token_details": {"cached_tokens": 8},
        }
    }
    assert parse_usage(env)["cached_tokens"] == 8


def test_parse_usage_none_when_absent():
    assert parse_usage({"type": "response.output_text.delta", "delta": "x"}) is None
    assert parse_usage("nope") is None  # type: ignore[arg-type]


def test_record_usage_accumulates():
    led: dict = {}
    record_usage(led, {"input_tokens": 100, "cached_tokens": 40, "output_tokens": 10, "total_tokens": 110})
    record_usage(led, {"input_tokens": 50, "cached_tokens": 50, "output_tokens": 5, "total_tokens": 55})
    assert led["judge_calls"] == 2
    assert led["judge_input_tokens"] == 150
    assert led["judge_cached_tokens"] == 90
    assert led["judge_output_tokens"] == 15
    assert led["judge_last_usage"]["cached_tokens"] == 50


def test_record_usage_ignores_bad_input():
    led = {"judge_calls": 3}
    record_usage(led, None)
    record_usage(led, {})  # empty usage still counts a call but adds zeros
    assert led["judge_calls"] == 4


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
