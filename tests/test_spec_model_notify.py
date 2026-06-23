#!/usr/bin/env python3
"""Spec CLI model notifications: stderr prefixes and PostToolUse forwarding."""

from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import model_notify as mn  # noqa: E402
from spec import save_spec, spec_template  # noqa: E402


def _sample_spec(*, judge_reason: str = "") -> dict:
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "Add density reinforcement to prompt adaptations"
    spec["tasks"] = [
        {
            "id": "T1",
            "title": "Density reinforcement lines added",
            "check": "true",
            "status": "failed",
            "judge_reason": judge_reason,
        },
        {"id": "T2", "title": "Re-measure flash-lite", "check": "true", "status": "pending"},
        {"id": "T4", "title": "Verify capsule floor", "check": "true", "status": "pending", "added_by": "judge"},
    ]
    return spec


LONG_JUDGE = (
    "The passing conflict scan is good evidence that stance conflicts were not detected, "
    "but it is not sufficient evidence that density reinforcement was actually added."
)


def test_format_spec_status_shows_board_and_highlight_judge():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    text = mn.format_spec_status(spec, highlight_task="T1")
    assert "goal: Add density reinforcement" in text
    assert "[XX] T1 (req) Density reinforcement" in text
    assert "[--] T4 (req) Verify capsule floor" in text
    assert f"judge: {LONG_JUDGE}" in text
    assert "breaker: CLOSED" in text


def test_notify_spec_update_emits_headline_and_board_only():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(
            spec,
            "T1 check ran (exit 2); judge rejected the evidence.",
            highlight_task="T1",
        )
    err = buf.getvalue()
    assert mn.NOTIFY_PREFIX in err
    assert mn.STATUS_PREFIX in err
    assert mn.JUDGE_PREFIX not in err
    assert mn.HINT_PREFIX not in err
    assert LONG_JUDGE in err.replace("\\n", "\n")
    assert "T4 (req) Verify capsule floor" in err.replace("\\n", "\n")


def test_build_spec_context_from_output_roundtrip():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(
            spec,
            "T1 check ran (exit 2); judge rejected the evidence. Judge added T4.",
            highlight_task="T1",
        )
    combined = "stdout noise\n" + buf.getvalue()
    ctx = mn.build_spec_context_from_output(combined)
    assert ctx.startswith("unifable spec update:")
    assert "judge rejected the evidence" in ctx
    assert "Judge:" not in ctx
    assert "Hint:" not in ctx
    assert LONG_JUDGE in ctx
    assert "[--] T4" in ctx
    assert "breaker: CLOSED" in ctx


def test_format_spec_status_ignores_legacy_judge_hint_field():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    spec["tasks"][0]["judge_hint"] = "legacy hint should not render"
    text = mn.format_spec_status(spec, highlight_task="T1")
    assert f"judge: {LONG_JUDGE}" in text
    assert "legacy hint" not in text


def test_parse_spec_cli_invocation():
    sub, tid = mn.parse_spec_cli_invocation("unifable add-task --title x --check true")
    assert sub == "add-task"
    assert tid is None


def test_format_spec_status_multi_judge_with_show_judge_for():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    spec["tasks"].append(
        {
            "id": "T3",
            "title": "Another failed req",
            "check": "true",
            "status": "failed",
            "judge_reason": "Insufficient test coverage",
        }
    )
    text = mn.format_spec_status(spec, show_judge_for=frozenset({"T1", "T3"}))
    assert f"judge: {LONG_JUDGE}" in text
    assert "judge: Insufficient test coverage" in text
    assert "judge:" not in text.split("T2")[1].split("T3")[0]


DISPUTE_REJECT_REASON = "Rejected. The evidence does not prove impossibility."
DISPUTE_ACCEPT_REASON = "The upstream API has no such endpoint; genuinely impossible."


def test_build_stop_validate_context_dispute_rejected():
    spec = _sample_spec()
    spec["tasks"] = [
        {
            "id": "T5",
            "title": "impossible req",
            "check": "true",
            "status": "failed",
            "judge_reason": DISPUTE_REJECT_REASON,
        }
    ]
    headlines = ["T5: dispute rejected"]
    ctx, _ = mn.build_stop_validate_context(spec, headlines)
    assert ctx.startswith("unifable spec update (stop validation):")
    assert "T5 [XX] impossible req" in ctx
    # judge reason rides the unresolved action inline, exactly once.
    assert DISPUTE_REJECT_REASON in ctx
    assert ctx.count(DISPUTE_REJECT_REASON) == 1
    assert "breaker: CLOSED" in ctx


def test_build_stop_validate_context_dispute_accepted():
    spec = _sample_spec()
    spec["tasks"] = [
        {
            "id": "T6",
            "title": "impossible req",
            "check": "true",
            "status": "retracted",
            "judge_reason": DISPUTE_ACCEPT_REASON,
        }
    ]
    headlines = ["T6 retracted — judge accepted impossibility. Completion breaker open."]
    ctx, _ = mn.build_stop_validate_context(spec, headlines)
    assert "Action required:" not in ctx
    assert "T6" not in ctx
    assert DISPUTE_ACCEPT_REASON not in ctx
    assert "breaker: OPEN" in ctx


def test_build_stop_validate_context_check_rejected():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    headlines = ["T1 check ran (exit 1); judge rejected the evidence."]
    ctx, _ = mn.build_stop_validate_context(spec, headlines)
    assert "T1 [XX] Density reinforcement" in ctx
    assert LONG_JUDGE in ctx
    assert ctx.count(LONG_JUDGE) == 1
    assert "breaker: CLOSED" in ctx


def test_build_stop_validate_context_no_judge_duplication():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    headlines = ["T1 check ran (exit 1); judge rejected the evidence."]
    ctx, _ = mn.build_stop_validate_context(spec, headlines)
    # The judge reason must appear once in the unresolved action list.
    assert ctx.count(LONG_JUDGE) == 1
    assert "Action required:" in ctx
    assert "T1 judge:" not in ctx


def test_format_spec_status_collapses_resolved():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    spec["tasks"] = [
        {"id": "T1", "title": "alpha", "check": "true", "status": "validated"},
        {"id": "T2", "title": "beta", "check": "true", "status": "validated"},
        {"id": "T3", "title": "gamma", "check": "true", "status": "validated"},
        {"id": "T4", "title": "delta", "check": "true", "status": "validated"},
        {"id": "T5", "title": "still failing", "check": "true", "status": "failed",
         "judge_reason": LONG_JUDGE},
        {"id": "T6", "title": "still pending", "check": "true", "status": "pending"},
    ]
    collapsed = mn.format_spec_status(spec, show_judge_for=frozenset({"T5"}), collapse_resolved=True)
    # resolved tasks fold into one done-count line; their titles are gone
    assert "done (4): T1, T2, T3, T4" in collapsed
    assert "alpha" not in collapsed
    assert "delta" not in collapsed
    # incomplete tasks keep their full rows
    assert "[XX] T5 (req) still failing" in collapsed
    assert "[--] T6 (req) still pending" in collapsed
    # the human `unifable status` CLI path (default) keeps every row in full
    full = mn.format_spec_status(spec)
    assert "[OK] T1 (req) alpha" in full
    assert "[OK] T4 (req) delta" in full
    assert "done (" not in full


def test_stop_context_omits_resolved_tasks():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    spec["tasks"] = [
        {"id": "T1", "title": "old done", "check": "true", "status": "validated"},
        {"id": "T2", "title": "freshly retracted", "check": "true", "status": "retracted",
         "judge_reason": DISPUTE_ACCEPT_REASON},
    ]
    headlines = ["T2 retracted — judge accepted impossibility."]
    ctx, _ = mn.build_stop_validate_context(spec, headlines)
    assert "done (" not in ctx
    assert "old done" not in ctx
    assert "freshly retracted" not in ctx
    assert DISPUTE_ACCEPT_REASON not in ctx
    assert "breaker: OPEN" in ctx


def test_spec_board_not_duplicated_across_channels():
    """gate_stop._attach_validate_context puts the board in additionalContext
    only; the short alarm stays in reason (no cross-channel duplication)."""
    import gate_stop

    board = (
        "unifable spec update (stop validation):\n"
        "  [XX] T1 (req) something\nbreaker: CLOSED (1 left: T1)"
    )
    payload = {"decision": "block", "reason": "breaker CLOSED: 1 task(s) not validated (T1)."}
    gate_stop._attach_validate_context(payload, board)
    ctx = (payload.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert board in ctx                       # board rides additionalContext
    assert board not in payload["reason"]     # not duplicated into reason
    assert "breaker CLOSED" in payload["reason"]  # alarm stays in reason


def test_collapse_already_done_tasks_to_count():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    spec["tasks"] = [
        {"id": "T1", "title": "alpha", "check": "true", "status": "validated"},
        {"id": "T2", "title": "beta", "check": "true", "status": "validated"},
        {"id": "T3", "title": "still failing", "check": "true", "status": "failed",
         "judge_reason": "needs more"},
    ]
    out = mn.format_spec_status(spec, show_judge_for=frozenset({"T3"}), collapse_resolved=True)
    assert "done (2): T1, T2" in out
    assert "alpha" not in out
    assert "[XX] T3" in out


def test_human_unifable_status_cli_full():
    """The human `unifable status` CLI path (collapse_resolved default False)
    renders every resolved task in full -- no collapsing."""
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    spec["tasks"] = [
        {"id": "T1", "title": "alpha", "check": "true", "status": "validated"},
        {"id": "T2", "title": "beta", "check": "true", "status": "validated"},
    ]
    full = mn.format_spec_status(spec)
    assert "[OK] T1 (req) alpha" in full
    assert "[OK] T2 (req) beta" in full
    assert "done (" not in full


def test_build_spec_context_from_output_ignores_legacy_judge_prefix_lines():
    combined = "\n".join(
        [
            f"{mn.NOTIFY_PREFIX}headline one",
            f"{mn.JUDGE_PREFIX}legacy duplicate judge line",
            f"{mn.STATUS_PREFIX}goal: g\\n  [--] T1 (req) x\\nbreaker: CLOSED (1 left: T1)",
        ]
    )
    ctx = mn.build_spec_context_from_output(combined)
    assert "headline one" in ctx
    assert "legacy duplicate judge line" not in ctx
    assert "Judge:" not in ctx
    assert "breaker: CLOSED" in ctx


def _run_post_tool(payload: dict) -> dict:
    import gate_post_tool

    with patch.object(gate_post_tool, "read_stdin_json", lambda: payload):
        with patch.object(gate_post_tool, "emit_json") as emit:
            gate_post_tool.main()
            if emit.call_count:
                return emit.call_args[0][0]
            return {}


def test_post_tool_forwards_failed_validate_task_stderr():
    spec = _sample_spec(judge_reason=LONG_JUDGE)
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(
            spec,
            "T1 check ran (exit 2); judge rejected the evidence. Judge added T4, T5.",
            highlight_task="T1",
        )
    stderr = buf.getvalue()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        payload = {
            "session_id": "spec-notify-test",
            "cwd": tmp,
            "tool_name": "Bash",
            "tool_input": {
                "command": "unifable add-task --title x --check true",
            },
            "tool_response": {
                "exit_code": 2,
                "stdout": "T1 -> failed\njudge added requirement(s): T4, T5",
                "stderr": stderr,
            },
        }
        out = _run_post_tool(payload)
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "unifable spec update:" in ctx
    assert "judge rejected the evidence" in ctx
    assert LONG_JUDGE in ctx
    assert "T4" in ctx
    assert "breaker: CLOSED" in ctx
    assert "observed a tool failure" not in ctx


def test_post_tool_add_task_reload_fallback_when_stderr_missing():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        spec = _sample_spec(judge_reason=LONG_JUDGE)
        save_spec(tmp, "sess-reload", spec)
        payload = {
            "session_id": "sess-reload",
            "cwd": tmp,
            "tool_name": "Bash",
            "tool_input": {
                "command": "unifable add-task --title x --check true",
            },
            "tool_response": {"exit_code": 0, "stdout": "Added T9: x"},
        }
        out = _run_post_tool(payload)
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        assert "unifable spec update:" in ctx
        assert "[XX] T1" in ctx
        assert "[--] T4" in ctx


def test_post_tool_add_task_success_no_failure_nag():
    spec = _sample_spec()
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(spec, "Requirement T9 added: new req.", highlight_task="T9")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        payload = {
            "session_id": "spec-add-test",
            "cwd": tmp,
            "tool_name": "Bash",
            "tool_input": {
                "command": "unifable add-task --title new --check true",
            },
            "tool_response": {"exit_code": 0, "stdout": "Added T9", "stderr": buf.getvalue()},
        }
        out = _run_post_tool(payload)
    ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    assert "Requirement T9 added" in ctx
    assert "observed a tool failure" not in ctx


STALE_JUDGE = "x" * 800
T17_HINT = "Run the isolated behavioral test as the real proof."
T18_HINT = "Show the gated logic in context, not only a string grep."


def test_collapse_stop_headlines_loop_release_batch():
    reason = "completion_stop_blocks is elevated and judge-added tasks are duplicates"
    headlines = [f"Judge retracted T{i}: {reason}" for i in range(6, 10)]
    collapsed = mn.collapse_stop_headlines(headlines)
    assert len(collapsed) == 1
    assert "T6-T9 (loop release)" in collapsed[0]
    assert reason in collapsed[0]


def test_stop_action_digest_before_stale_items():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    tasks = []
    for i in range(1, 10):
        tasks.append({
            "id": f"T{i}",
            "title": f"stale {i}",
            "check": "true",
            "status": "failed",
            "judge_reason": STALE_JUDGE,
        })
    tasks.extend([
        {
            "id": "T17",
            "title": "behavioral proof",
            "check": "true",
            "status": "failed",
            "judge_reason": f"Check passed but evidence is non-probative. {T17_HINT}",
        },
        {
            "id": "T18",
            "title": "grep only",
            "check": "true",
            "status": "failed",
            "judge_reason": f"String grep alone is insufficient. {T18_HINT}",
        },
    ])
    spec["tasks"] = tasks
    headlines = [
        "T17 check ran (exit 0); judge rejected the evidence.",
        "T18 check ran (exit 0); judge rejected the evidence.",
    ]
    ctx, _ = mn.build_stop_validate_context(spec, headlines)
    action_pos = ctx.find("Action required:")
    t17_hint_pos = ctx.find(T17_HINT)
    stale_pos = ctx.find(STALE_JUDGE)
    assert action_pos >= 0
    assert t17_hint_pos >= 0
    assert t17_hint_pos < action_pos + 2500
    assert stale_pos == -1
    assert "Board:" not in ctx
    assert ctx.find("Action required:") < ctx.find("breaker: CLOSED")


def test_stop_context_prioritizes_hints_in_first_2kb():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    tasks = []
    for i in range(1, 16):
        tasks.append({
            "id": f"T{i}",
            "title": f"stale {i}",
            "check": "true",
            "status": "failed",
            "judge_reason": STALE_JUDGE,
        })
    tasks.append({
        "id": "T17",
        "title": "needs behavioral test",
        "check": "true",
        "status": "failed",
        "judge_reason": f"Non-probative grep. {T17_HINT}",
    })
    spec["tasks"] = tasks
    headlines = ["T17 check ran (exit 0); judge rejected the evidence."]
    ctx, _ = mn.build_stop_validate_context(spec, headlines)
    assert T17_HINT in ctx[:2048]


def test_format_blocking_task_hints_prioritizes_changed():
    """Action lines cover tasks adjudicated this stop only, not stale siblings."""
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["tasks"] = [
        {"id": "T1", "title": "old", "check": "true", "status": "failed", "judge_reason": "stale"},
        {
            "id": "T17",
            "title": "new",
            "check": "true",
            "status": "failed",
            "judge_hint": T17_HINT,
        },
    ]
    text = mn.format_blocking_task_hints(
        spec, ["T1", "T17"], changed_ids={"T17"},
    )
    assert "Action:" in text
    assert T17_HINT in text
    assert "T1:" not in text


def test_stop_context_omits_heavy_board_rows():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    spec["heavy_workflow"] = True
    spec["heavy_phase"] = "frontier"
    spec["tasks"] = [
        {
            "id": "T1",
            "title": "frontier A",
            "check": "true",
            "status": "failed",
            "approach_kind": "frontier",
            "judge_reason": "frontier still viable; run the targeted proof",
        },
        {
            "id": "T2",
            "title": "frontier B",
            "check": "true",
            "status": "pending",
            "approach_kind": "frontier",
        },
        {
            "id": "T3",
            "title": "primary fallback",
            "check": "true",
            "status": "blocked",
            "approach_kind": "primary",
        },
    ]
    headlines = ["T1 check ran (exit 1); judge rejected the evidence."]
    ctx, _ = mn.build_stop_validate_context(spec, headlines)
    assert "heavy_phase:" not in ctx
    assert "[frontier]" not in ctx
    assert "[primary]" not in ctx
    assert "done (" not in ctx
    assert "T1 [XX] frontier A" in ctx
    assert "frontier still viable" in ctx


def test_build_stop_validate_context_truncation_flag():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    spec["tasks"] = [
        {
            "id": "T1",
            "title": "x",
            "check": "true",
            "status": "failed",
            "judge_reason": "r; do the thing",
        },
    ]
    headlines = ["T1 rejected"]
    ctx, truncated = mn.build_stop_validate_context(spec, headlines, max_len=100)
    assert truncated is True
    assert "do the thing" in ctx


def test_stop_unresolved_synthetic_primary_missing():
    from spec import append_frontier_task

    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "ship feature"
    append_frontier_task(spec, "Try WASM path", "cargo test -p wasm")
    append_frontier_task(spec, "Try native path", "cargo test -p native")
    ctx, _ = mn.build_stop_validate_context(spec, ["frontier explore"])
    assert "unifable set-primary" in ctx
    assert "primary approach (missing)" in ctx
    assert "<need primary approach task>" not in ctx


def test_stop_unresolved_synthetic_no_requirements():
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "g"
    spec["tasks"] = []
    ctx, _ = mn.build_stop_validate_context(spec, ["seed"])
    assert "unifable add-task" in ctx
    assert "requirements (none yet)" in ctx
    assert "<no requirements added yet>" not in ctx


def test_format_blocking_task_hints_synthetic_incomplete():
    from spec import append_frontier_task

    spec = spec_template()
    spec["requires_tasks"] = True
    append_frontier_task(spec, "Frontier A", "true")
    append_frontier_task(spec, "Frontier B", "true")
    hints = mn.format_blocking_task_hints(spec, ["<need primary approach task>"])
    assert "unifable set-primary" in hints
    hints2 = mn.format_blocking_task_hints(spec, ["<need >=2 frontier approach tasks>"])
    assert "unifable add-frontier" in hints2
