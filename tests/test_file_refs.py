#!/usr/bin/env python3
"""Tests for pointer + rehydrate file references in judge directive/steering.

The judge truncates long filenames when it retypes them in prose
(research_bash_guidance.py -> ash_guidance.py). The fix: hand the judge a numbered
FILE INDEX, have it reference files by [[n]], and rehydrate the pointers host-side.
These tests pin the lossless mechanism (not a brittle output-text heuristic).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import file_refs as fr  # noqa: E402
import groundedness as gb  # noqa: E402


# --- extract_paths / build_file_index --------------------------------------
def test_extract_paths_first_seen_order_and_dedup() -> None:
    seg = (
        "Read scripts/gate/research_bash_guidance.py then tests/test_spec_facade_api.py; "
        "again scripts/gate/research_bash_guidance.py is the target."
    )
    assert fr.extract_paths(seg) == [
        "scripts/gate/research_bash_guidance.py",
        "tests/test_spec_facade_api.py",
    ]


def test_build_file_index_empty_when_no_paths() -> None:
    text, paths = fr.build_file_index("no files mentioned here, just prose about T1")
    assert text == "" and paths == []


def test_build_file_index_numbers_paths_and_explains_pointer() -> None:
    text, paths = fr.build_file_index(
        "look at scripts/gate/groundedness_facade_api.py and tests/test_spec_facade_api.py"
    )
    assert paths == [
        "scripts/gate/groundedness_facade_api.py",
        "tests/test_spec_facade_api.py",
    ]
    assert "[0] scripts/gate/groundedness_facade_api.py" in text
    assert "[1] tests/test_spec_facade_api.py" in text
    assert "[[" in text and "double brackets" in text  # tells the judge the convention


def test_build_file_index_caps_to_max() -> None:
    seg = " ".join(f"dir/mod_{i}.py" for i in range(fr.MAX_INDEX_FILES + 25))
    _text, paths = fr.build_file_index(seg)
    assert len(paths) == fr.MAX_INDEX_FILES


# --- rehydrate_file_refs ----------------------------------------------------
def test_rehydrate_resolves_pointers_to_full_paths() -> None:
    paths = ["scripts/gate/research_bash_guidance.py", "scripts/gate/groundedness_facade_api.py"]
    # The judge could not have truncated: it emitted an integer, not the name.
    out = fr.rehydrate_file_refs("Read [[0]] to confirm behavior before editing [[1]].", paths)
    assert out == (
        "Read scripts/gate/research_bash_guidance.py to confirm behavior before "
        "editing scripts/gate/groundedness_facade_api.py."
    )
    assert "ash_guidance.py" not in out.replace("research_bash_guidance.py", "")


def test_rehydrate_tolerates_inner_whitespace() -> None:
    assert fr.rehydrate_file_refs("Read [[ 0 ]].", ["a/b.py"]) == "Read a/b.py."


def test_rehydrate_out_of_range_pointer_left_verbatim() -> None:
    # Fail safe: a bad index is never resolved to the wrong file.
    assert fr.rehydrate_file_refs("Read [[5]].", ["a/b.py"]) == "Read [[5]]."


def test_rehydrate_passthrough_without_pointers() -> None:
    assert fr.rehydrate_file_refs("Run the failing check and paste output.", ["a/b.py"]) == (
        "Run the failing check and paste output."
    )
    assert fr.rehydrate_file_refs("", ["a/b.py"]) == ""


# --- arm_judge integration: index appended + pointers rehydrated ------------
class _PointerJudge:
    """Judge stub that references files by pointer and records the user message."""

    def __init__(self, directive: str, steering: str = "") -> None:
        self.directive = directive
        self.steering = steering
        self.seen_user = ""

    def __call__(self, system: str, user: str, schema: dict) -> dict:
        self.seen_user = user
        return {
            "verdict": 1 if self.steering else 0,
            "load_bearing": 1 if self.steering else 0,
            "steering": self.steering,
            "claim": "x" if self.steering else "",
            "directive": self.directive,
            "tool_scope": {"allow": ["Read"], "deny": ["Edit"]},
        }


SEG = (
    "[agent] consolidating scripts/gate/research_bash_guidance.py and "
    "scripts/gate/groundedness_facade_api.py; tests in tests/test_spec_facade_api.py."
)


def test_arm_judge_appends_file_index_to_user_message() -> None:
    j = _PointerJudge(directive="Read [[0]].")
    out: dict = {}
    gb.arm_judge(SEG, judge=j, out=out)
    assert "FILE INDEX" in j.seen_user
    assert "[0] scripts/gate/research_bash_guidance.py" in j.seen_user


def test_arm_judge_rehydrates_directive_pointer() -> None:
    j = _PointerJudge(directive="Read [[0]] to confirm its responsibilities before editing [[1]].")
    out: dict = {}
    gb.arm_judge(SEG, judge=j, out=out)
    assert out["directive"] == (
        "Read scripts/gate/research_bash_guidance.py to confirm its responsibilities "
        "before editing scripts/gate/groundedness_facade_api.py."
    )


def test_arm_judge_rehydrates_steering_pointer() -> None:
    j = _PointerJudge(
        directive="Read [[0]].",
        steering="Claim unproven; read [[0]] to confirm before editing.",
    )
    verdict, steering, claim = gb.arm_judge(SEG, judge=j)
    assert "scripts/gate/research_bash_guidance.py" in steering
    assert "[[0]]" not in steering


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
