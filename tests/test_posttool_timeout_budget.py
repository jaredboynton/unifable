#!/usr/bin/env python3
"""PostToolUse timeout budget: the fan-out can never be killed mid-judge.

Regression for the codex-thread "hook timed out after 10s": the host PostToolUse
budget must comfortably exceed the concurrent judge fan-out's wall-clock budget,
and a single judge round-trip (handshake + read) must fit under the host budget so
a slow judge returns a clean JudgeError instead of a host kill. Mirrors
tests/test_stop_timeout_budget.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import codex_judge  # noqa: E402
from posttool_judges import POSTTOOL_JUDGE_BUDGET  # noqa: E402

MANIFESTS = ["hooks/hooks.json", ".codex-plugin/hooks.json"]


def _post_tool_timeout(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    for group in data["hooks"]["PostToolUse"]:
        for hook in group["hooks"]:
            if "gate_post_tool" in hook["command"]:
                return int(hook["timeout"])
    raise AssertionError(f"no gate_post_tool hook in {path}")


def test_manifests_post_tool_timeout_covers_budget():
    for rel in MANIFESTS:
        host = _post_tool_timeout(REPO / rel)
        assert host >= POSTTOOL_JUDGE_BUDGET, rel


def test_post_tool_timeout_meets_floor():
    # Pin the 120s floor so a future edit cannot quietly drop PostToolUse back toward
    # the old 10s that killed the hook mid-judge.
    for rel in MANIFESTS:
        assert _post_tool_timeout(REPO / rel) >= 120, rel


def test_single_judge_call_fits_under_host_budget():
    # The fan-out runs judges concurrently, so wall clock is ~one judge round-trip;
    # handshake + read must finish before the host kills the hook.
    host = min(_post_tool_timeout(REPO / rel) for rel in MANIFESTS)
    assert codex_judge.HANDSHAKE_TIMEOUT + codex_judge.READ_TIMEOUT <= host


def test_budget_is_positive_and_bounded():
    host = min(_post_tool_timeout(REPO / rel) for rel in MANIFESTS)
    assert 0 < POSTTOOL_JUDGE_BUDGET <= host
