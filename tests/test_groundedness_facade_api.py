#!/usr/bin/env python3
"""Facade-API guard for scripts/gate/groundedness.py.

groundedness.py is split into focused sub-modules (breaker_filters, breaker_prompts,
breaker_runtime, breaker_judges, breaker_orchestration) and kept as a thin re-export
facade. This test pins the public + test-relied import surface so a missing
re-export fails loudly the moment a symbol drops off the facade.

Runs under pytest or standalone (python3 tests/test_groundedness_facade_api.py).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import groundedness as gb  # noqa: E402

FACADE_SYMBOLS = (
    # hook entry points
    "evaluate_pre_tool",
    "evaluate_pre_tool_locked",
    "evaluate_post_tool_release",
    "evaluate",
    # tool classification
    "is_mutation_tool",
    "is_release_tool",
    # judges
    "arm_judge",
    "disarm_judge",
    "monitor_provisional_judge",
    "judge_segment",
    "verify_claim_predicate",
    # director / state
    "breaker_key",
    "arm",
    "record_verdict",
    "DIRECTIVE_MAX_CHARS",
    "JUDGE_WINDOW_SECONDS",
    # transcript
    "judge_transcript",
    "transcript_segment",
    "adjudicated_claims",
    # claim filters
    "is_harness_self_referential",
    "is_task_board_status_claim",
    "loaded_skill_names",
    "claim_describes_loaded_skill",
    # prompt / board constants
    "_JUDGE_SYSTEM",
    "_SPEC_BOARD_BEGIN",
    "_SPEC_BOARD_END",
    "MUTATION_TOOLS",
    "RELEASE_TOOLS",
)


def test_facade_exports_all_symbols():
    missing = [name for name in FACADE_SYMBOLS if not hasattr(gb, name)]
    assert not missing, f"groundedness facade is missing re-exports: {missing}"


def test_evaluate_alias_points_at_pre_tool():
    assert gb.evaluate is gb.evaluate_pre_tool


if __name__ == "__main__":
    test_facade_exports_all_symbols()
    test_evaluate_alias_points_at_pre_tool()
    print("OK: groundedness facade exposes", len(FACADE_SYMBOLS), "symbols")
