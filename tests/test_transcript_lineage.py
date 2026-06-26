#!/usr/bin/env python3
"""Unit tests for the breaker task-lineage fingerprint (scripts/gate/transcript_tail.py).

The fingerprint is the breaker's fallback task-boundary signal when the ledger's
per-prompt active_task is empty (the common production case, e.g. post-/compact).
It MUST be stable within one task and distinct across tasks, and MUST reject
tool-result user turns (which recur within a task) so the key never churns.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "gate"))
from transcript_tail import (  # noqa: E402
    _is_human_user_turn,
    latest_user_prompt_fingerprint,
)


def _write_jsonl(records):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for r in records:
        f.write(json.dumps(r) + "\n")
    f.flush()
    f.close()
    return f.name


def test_human_str_turn_accepted():
    assert _is_human_user_turn({"message": {"role": "user", "content": "fix the parser"}}) == "fix the parser"


def test_tool_result_turn_rejected():
    rec = {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "out"}]}}
    assert _is_human_user_turn(rec) == ""


def test_assistant_turn_rejected():
    assert _is_human_user_turn({"message": {"role": "assistant", "content": "hi"}}) == ""


def test_text_block_list_accepted():
    rec = {"message": {"role": "user", "content": [{"type": "text", "text": "plan the agenda"}]}}
    assert _is_human_user_turn(rec) == "plan the agenda"


def test_fingerprint_stable_within_task_distinct_across_tasks():
    base = [
        {"type": "user", "message": {"role": "user", "content": "task A: fix the parser"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "o"}]}},
    ]
    path = _write_jsonl(base)
    try:
        fp1 = latest_user_prompt_fingerprint(path)
        assert fp1 and len(fp1) == 16
        # More tool-result turns in the SAME task must not change the fingerprint.
        with open(path, "a", encoding="utf-8") as fh:
            for _ in range(3):
                fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "y", "content": "o2"}]}}) + "\n")
        fp2 = latest_user_prompt_fingerprint(path)
        assert fp2 == fp1, "fingerprint must be stable across tool turns within a task"
        # A new HUMAN prompt (task B) must change the fingerprint.
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": "task B: plan the agenda"}}) + "\n")
        fp3 = latest_user_prompt_fingerprint(path)
        assert fp3 and fp3 != fp1, "a new human prompt must yield a distinct fingerprint"
    finally:
        os.unlink(path)


def test_missing_or_empty_transcript_returns_empty():
    assert latest_user_prompt_fingerprint(None) == ""
    assert latest_user_prompt_fingerprint("/no/such/file.jsonl") == ""
    empty = _write_jsonl([])
    try:
        assert latest_user_prompt_fingerprint(empty) == ""
    finally:
        os.unlink(empty)


def test_only_tool_results_returns_empty():
    """A transcript with no human turn (only tool results) yields no fingerprint,
    so the caller falls back to the empty component -- no regression vs today."""
    recs = [
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "o"}]}},
    ]
    path = _write_jsonl(recs)
    try:
        assert latest_user_prompt_fingerprint(path) == ""
    finally:
        os.unlink(path)


if __name__ == "__main__":
    fails = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"  [OK] {_name}")
            except AssertionError as e:
                fails += 1
                print(f"  [FAIL] {_name}: {e}")
    print("RESULT:", "all pass" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
