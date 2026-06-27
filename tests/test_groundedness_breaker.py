#!/usr/bin/env python3
"""Logic tests for the overconfidence / groundedness breaker (groundedness.py).

Each test maps to a requirement of the breaker:
  R1  block ONLY Write/Edit/Bash; never WebSearch/Read/WebFetch/Grep/Glob
  R2  arm (strict judge, verdict 1) and disarm (separate claim-bound release judge)
  R5  judge question = "did the model say something confidently w/o backing it up"
  R6  verdict 1 -> steering prompt returned + mutation blocked until evidence read
  R7  verdict 0 -> no steering, model sees nothing (no block)
  R8  release via PostToolUse after Read/WebFetch with fresh tool output
  R9  arm judge debounced: <=1 call / 3s per session+user-prompt key
  R10 safety cap: after max_blocks consecutive blocks on one arm, fail open
Run: python3 -m pytest tests/test_groundedness_breaker.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import groundedness as gb  # noqa: E402
from breaker_state import adjudicated_claims, append_event, default_breaker, render_events  # noqa: E402


class FakeJudge:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.systems = []

    def __call__(self, system, user, schema):
        self.systems.append(system)
        self.calls += 1
        entry = self.script[min(self.calls - 1, len(self.script) - 1)]
        if len(entry) == 3:
            verdict, steering, load_bearing = entry
        else:
            verdict, steering = entry
            load_bearing = 1 if verdict == 1 else 0
        return {
            "verdict": verdict,
            "steering": steering,
            "claim": "claim" if verdict == 1 else "",
            "load_bearing": load_bearing,
        }


class RoutingJudge:
    def __init__(
        self,
        arm=(1, "blocked", "the cause is Y"),
        grounded=1,
        needed="read foo.py:10 and cite it",
        load_bearing=1,
        release_load_bearing=1,
        provisional_release=0,
        lift_reason="",
        lift_scope="",
        monitor_drift_level=0,
        monitor_feedback="",
    ):
        self.arm_ret = arm
        self.grounded = grounded
        self.needed = needed
        self.load_bearing = load_bearing
        self.release_load_bearing = release_load_bearing
        self.provisional_release = provisional_release
        self.lift_reason = lift_reason
        self.lift_scope = lift_scope
        self.monitor_drift_level = monitor_drift_level
        self.monitor_feedback = monitor_feedback
        self.arm_calls = 0
        self.disarm_calls = 0
        self.monitor_calls = 0

    def __call__(self, system, user, schema):
        if "provisional-lift monitor" in system.lower():
            self.monitor_calls += 1
            drift = self.monitor_drift_level
            if drift == 1:
                return {"drift_level": 1, "feedback": self.monitor_feedback}
            if drift == 2:
                return {"drift_level": 2, "feedback": self.monitor_feedback}
            return {"drift_level": 0, "feedback": ""}
        if "release monitor" in system.lower():
            self.disarm_calls += 1
            lb = self.release_load_bearing
            if self.grounded:
                return {
                    "grounded": 1,
                    "needed": "",
                    "load_bearing": lb,
                    "provisional_release": 0,
                    "lift_reason": "",
                    "lift_scope": "",
                }
            if self.provisional_release:
                return {
                    "grounded": 0,
                    "needed": "",
                    "load_bearing": lb,
                    "provisional_release": 1,
                    "lift_reason": self.lift_reason,
                    "lift_scope": self.lift_scope,
                }
            return {
                "grounded": 0,
                "needed": self.needed,
                "load_bearing": lb,
                "provisional_release": 0,
                "lift_reason": "",
                "lift_scope": "",
            }
        self.arm_calls += 1
        v, s, c = self.arm_ret
        return {"verdict": v, "steering": s, "claim": c, "load_bearing": self.load_bearing if v == 1 else 0}


def _pre(tool, session="S"):
    return {"tool_name": tool, "session_id": session, "cwd": "/repo"}


def _state():
    return default_breaker()


def test_mutation_set_is_exactly_writes_edits_bash():
    for t in ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "Bash", "REPL", "exec_command"):
        assert gb.is_mutation_tool(t), t
    for t in ("WebSearch", "Read", "WebFetch", "Grep", "Glob", "Task", "TodoWrite"):
        assert not gb.is_mutation_tool(t), t


def test_release_tools_include_reads_and_fetch():
    for t in ("Read", "WebFetch", "WebSearch", "Grep", "Glob", "NotebookRead"):
        assert gb.is_release_tool(t), t


def test_whitelisted_bash_is_release_tool_with_input():
    assert gb.is_release_tool(
        "Bash",
        {"tool_name": "Bash", "tool_input": {"command": "rg '1.9.90' .claude-plugin/ | wc -l"}},
    )
    assert gb.is_release_tool(
        "Bash",
        {"tool_name": "Bash", "tool_input": {"command": "wc -l setup/setup.sh"}},
    )
    assert not gb.is_release_tool("Bash")
    assert not gb.is_release_tool(
        "Bash",
        {"tool_name": "Bash", "tool_input": {"command": "npm test"}},
    )


def test_whitelisted_exec_command_is_release_tool_with_input():
    assert gb.is_release_tool(
        "exec_command",
        {"tool_name": "exec_command", "tool_input": {"cmd": "rg -n foo src/"}},
    )
    assert not gb.is_release_tool(
        "exec_command",
        {"tool_name": "exec_command", "tool_input": {"cmd": "npm test"}},
    )


def test_whitelisted_exec_js_tools_exec_command_is_release_tool():
    aw = "a" + "w" + "ait"
    assert gb.is_release_tool(
        "exec",
        {
            "tool_name": "exec",
            "tool_input": {
                "code": f"{aw} tools.exec_command({{ cmd: 'rg -n pat hooks/hooks.json' }})"
            },
        },
    )
    assert not gb.is_release_tool(
        "exec",
        {
            "tool_name": "exec",
            "tool_input": {"code": f"{aw} tools.exec_command({{ cmd: 'npm test' }})"},
        },
    )


def test_view_image_is_release_tool():
    assert gb.is_release_tool("view_image")
    assert gb.is_release_tool(
        "view_image",
        {"tool_name": "view_image", "tool_input": {"path": "assets/x.png"}},
    )


def test_mcp_read_like_with_path_is_release_tool():
    assert gb.is_release_tool(
        "mcp__octocode__githubGetFileContent",
        {
            "tool_name": "mcp__octocode__githubGetFileContent",
            "tool_input": {"queries": [{"path": "src/x.py"}]},
        },
    )
    assert not gb.is_release_tool(
        "mcp__foo__createIssue",
        {"tool_name": "mcp__foo__createIssue", "tool_input": {"path": "src/x.py"}},
    )


def _repl_code(expr: str) -> str:
    aw = "a" + "w" + "ait"
    return f"{aw} {expr}"


def test_repl_read_code_is_release_tool():
    assert gb.is_release_tool(
        "REPL",
        {"tool_name": "REPL", "tool_input": {"code": _repl_code('Read({file_path: "src/x.py"})')}},
    )
    assert gb.is_release_tool(
        "REPL",
        {
            "tool_name": "REPL",
            "tool_input": {"code": _repl_code('Bash({command: "rg -n pat hooks/"})')},
        },
    )
    assert not gb.is_release_tool(
        "REPL",
        {"tool_name": "REPL", "tool_input": {"code": _repl_code('Bash({command: "npm test"})')}},
    )


def test_arms_then_disarms_via_whitelisted_bash_post_tool_release(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "unproven; blocked", "nine version fields say 1.9.90"), grounded=1)
    state = _state()
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    bash_input = {
        "tool_name": "Bash",
        "session_id": "S",
        "cwd": "/repo",
        "tool_input": {"command": "rg '1.9.90' .claude-plugin/ .codex-plugin/"},
    }
    grounded, needed, message = gb.evaluate_post_tool_release(
        bash_input,
        state,
        fresh_tool="[tool_result name=Bash]\n.claude-plugin/plugin.json:3:  \"version\": \"1.9.90\"",
        judge=judge,
    )
    assert grounded is True and needed == "" and "claim grounded" in message.lower()
    assert state["breaker_armed"] is False
    assert judge.disarm_calls == 1


def test_read_and_websearch_never_blocked_even_when_armed(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "model: definitely the cause is Y")
    judge = FakeJudge([(1, "you claimed Y with no proof; mutation blocked")])
    state = _state()
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=100.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Read"), state, now=101.0, active_task="P", judge=judge)
    assert blocked is False and steering == ""
    blocked, _, _ = gb.evaluate_pre_tool(_pre("WebSearch"), state, now=102.0, active_task="P", judge=judge)
    assert blocked is False


def test_verdict1_blocks_mutation_and_returns_steering(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "model: the fix is obviously Z")
    judge = FakeJudge([(1, "You asserted Z without reading the source. Write/Edit/Bash blocked until you cite evidence.")])
    state = _state()
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=10.0, active_task="P", judge=judge)
    assert blocked is True
    assert "blocked" in steering.lower() and steering != ""


def test_verdict0_no_block_no_steering(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "model: I read foo.py:10, it does X; here is the edit")
    judge = FakeJudge([(0, "")])
    state = _state()
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Write"), state, now=10.0, active_task="P", judge=judge)
    assert blocked is False and steering == ""
    assert state["breaker_armed"] is False


def test_arms_then_disarms_via_post_tool_release(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "unproven; blocked", "the cause is Y"), grounded=1)
    state = _state()
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    assert judge.arm_calls == 1 and judge.disarm_calls == 0
    grounded, needed, message = gb.evaluate_post_tool_release(
        _pre("Read"), state, fresh_tool="[tool_result name=Read]\nevidence", judge=judge
    )
    assert grounded is True and needed == "" and "claim grounded" in message.lower()
    assert state["breaker_armed"] is False
    assert judge.disarm_calls == 1


def test_armed_stays_blocked_on_mutation_without_post_tool_release(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "blocked", "claim X"), grounded=0, release_load_bearing=1)
    state = _state()
    gb.evaluate_pre_tool(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    assert state["breaker_armed"] is True
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=99.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    assert judge.disarm_calls == 1


def test_post_tool_release_not_grounded_stays_armed(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "blocked", "claim X"), grounded=0, needed="still missing: read codex_judge.py:54 and cite MODEL")
    state = _state()
    gb.evaluate_pre_tool(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    grounded, needed, message = gb.evaluate_post_tool_release(
        _pre("Read"), state, fresh_tool="[tool_result name=Read]\nwrong file", judge=judge
    )
    assert grounded is False and state["breaker_armed"] is True
    assert judge.disarm_calls == 1
    assert needed == "still missing: read codex_judge.py:54 and cite MODEL"
    assert "claim still ungrounded" in message.lower()
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    assert blocked is True
    assert steering == needed


def test_arm_judge_debounced_at_most_once_per_3s_per_key(monkeypatch):
    # Stepwise harness: the per-tool judge debounce tightened from 15s to 3s.
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "t")
    judge = RoutingJudge(arm=(0, "", ""))
    state = _state()
    gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    gb.evaluate_pre_tool(_pre("Bash"), state, now=1.0, active_task="P", judge=judge)
    gb.evaluate_pre_tool(_pre("Bash"), state, now=2.9, active_task="P", judge=judge)
    assert judge.arm_calls == 1
    gb.evaluate_pre_tool(_pre("Bash"), state, now=3.0, active_task="P", judge=judge)
    assert judge.arm_calls == 2


def test_safety_cap_fails_open_after_max_blocks(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    monkeypatch.setenv("UNIFABLE_BREAKER_MAX_BLOCKS", "3")
    judge = RoutingJudge(arm=(1, "blocked", "claim X"), grounded=0)
    state = _state()
    b1, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    b2, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    b3, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=2.0, active_task="P", judge=judge)
    assert b1 is True and b2 is True
    assert b3 is False and state["breaker_armed"] is False


def test_new_user_prompt_drops_stale_arm(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(0, "", ""))
    state = _state()
    gb.arm(state, gb.breaker_key("S", "P1"), 0.0, "blocked", "claim X")
    assert state["breaker_armed"] is True
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit", session="S"), state, now=1.0, active_task="P2", judge=judge)
    assert blocked is False and state["breaker_armed"] is False
    assert judge.arm_calls == 1
    assert any(e.get("kind") == "STALE_ARM_DROPPED" for e in state["events"])


def test_new_prompt_drops_stale_arm_on_research_bash(monkeypatch):
    """A stale ARM from task A must clear on task B's first GATED tool even when
    that tool is a whitelisted research Bash -- not only on a mutation tool. The
    breaker matcher includes Bash, so a research Bash reaches evaluate_pre_tool;
    it must not eat a block from the previous task's arm."""
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(0, "", ""))
    state = _state()
    gb.arm(state, gb.breaker_key("S", "P1"), 0.0, "blocked", "claim X")
    assert state["breaker_armed"] is True
    research = {"tool_name": "Bash", "session_id": "S", "tool_input": {"command": "rg foo src/"}}
    blocked, _, _ = gb.evaluate_pre_tool(research, state, now=1.0, active_task="P2", judge=judge)
    assert blocked is False and state["breaker_armed"] is False
    assert any(e.get("kind") == "STALE_ARM_DROPPED" for e in state["events"])


def test_new_prompt_drops_stale_provisional_lift(monkeypatch):
    """A stale PROVISIONAL lift from task A must clear on task B's first gated tool.

    The provisional branch in evaluate_pre_tool returns early; without the stale
    drop preceding it, a task-A provisional lift would run the release/monitor
    judges against task B's transcript and surface task-A scope -- gating
    unrelated work. The drop must fire first so task B is judged fresh."""
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(0, "", ""))
    state = _state()
    # Seed a provisional lift owned by task A (key S|P1).
    from breaker_state import lift_provisional

    state["breaker_key"] = gb.breaker_key("S", "P1")
    lift_provisional(state, "claim A", "reason A", "scope A", "notify A")
    assert state["breaker_provisional"] is True
    # Task B's first mutation tool: stale drop fires, fresh arm-judge runs (verdict 0).
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit", session="S"), state, now=1.0, active_task="P2", judge=judge)
    assert blocked is False
    assert state["breaker_provisional"] is False
    assert any(e.get("kind") == "STALE_ARM_DROPPED" for e in state["events"])
    # The fresh arm-judge ran for task B (provisional branch did not short-circuit it).
    assert judge.arm_calls == 1
    # No task-A monitor/release judging happened against task B's transcript.
    assert judge.monitor_calls == 0


def test_empty_active_task_does_not_collapse_key_across_prompts(monkeypatch):
    """RC1: in production `active_task` is empty ~90% of the time when the breaker
    runs (gate_prompt has not re-pinned it, e.g. after /compact). With the old
    keying, breaker_key('S','') == 'S|' for BOTH prompts, so the stale-arm drop
    (which fires only on key change) never triggers and task A's arm leaks into
    task B. The breaker must derive task lineage from the transcript's latest user
    prompt when active_task is empty, so two distinct prompts get distinct keys."""
    # Lineage falls back to the latest-user-prompt fingerprint when active_task is
    # empty; mock it to return per-task fingerprints (the real extractor is unit
    # tested in test_transcript_lineage.py).
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    fp = {"v": "fpA"}
    monkeypatch.setattr("breaker_runtime.latest_user_prompt_fingerprint", lambda p: fp["v"], raising=False)
    # Task A transcript ends with user prompt 1; arm against it.
    judge_arm = RoutingJudge(arm=(1, "blocked: prove it", "the parser bug is X"))
    state = _state()
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit", session="S"), state, now=0.0, active_task="", judge=judge_arm)
    assert blocked is True and state["breaker_armed"] is True
    key_a = state["breaker_key"]
    # Task B: a DIFFERENT user prompt, active_task still empty (post-compact).
    fp["v"] = "fpB"
    judge_b = RoutingJudge(arm=(0, "", ""))
    pre_b = {"tool_name": "Edit", "session_id": "S", "cwd": "/repo",
             "transcript_path": "/tmp/does-not-exist-but-segment-mocked"}
    blocked_b, _, _ = gb.evaluate_pre_tool(pre_b, state, now=1.0, active_task="", judge=judge_b)
    # Task B must NOT be blocked by task A's arm: the keys must differ so the stale
    # drop fires (or the fresh judge runs), not collapse to a shared 'S|'.
    assert state["breaker_key"] != key_a, "empty active_task must not collapse two prompts to one key"
    assert blocked_b is False, "task A's stale arm must not gate task B"


def test_debounce_key_is_session_plus_user_prompt(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "t")
    judge = FakeJudge([(1, "blocked")])
    state = _state()
    gb.evaluate_pre_tool(_pre("Bash", session="S"), state, now=0.0, active_task="P1", judge=judge)
    gb.evaluate_pre_tool(_pre("Bash", session="S"), state, now=1.0, active_task="P2", judge=judge)
    assert judge.calls == 2
    gb.evaluate_pre_tool(_pre("Bash", session="S2"), state, now=1.5, active_task="P2", judge=judge)
    assert judge.calls == 3


def test_judge_exception_fails_open(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "t")

    def boom(system, user, schema):
        raise RuntimeError("realtime down")

    state = _state()
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=boom)
    assert blocked is False and steering == ""


def test_completion_stall_circuit_breaker_backstop_releases(monkeypatch):
    """The completion breaker is a bounded circuit breaker: on a stalled
    (no-net-progress) run of blocks it trips and releases Stop instead of
    trapping the session. Covers the unsat-budget stall-release backstop
    (stall / backstop / circuit). The shipped default cap is 0 (infinite); pin a
    finite cap here so the release-at-cap contract is exercised."""
    import verify_state as vs

    monkeypatch.setattr(vs, "COMPLETION_MAX_STALLED_BLOCKS", 6)
    led: dict = {}
    released = any(
        vs.note_completion_block(led, 8)  # constant incomplete count -> stalled
        for _ in range(vs.COMPLETION_MAX_STALLED_BLOCKS + 1)
    )
    assert released is True
    assert int(led["completion_stall_blocks"]) >= vs.COMPLETION_MAX_STALLED_BLOCKS


def test_non_load_bearing_explanation_does_not_arm(monkeypatch):
    monkeypatch.setattr(
        gb,
        "transcript_segment",
        lambda d, **k: (
            "user: update spec.py session keying\nassistant: TaskUpdate failed because the task list reset after plugin reload."
        ),
    )
    judge = RoutingJudge(
        arm=(1, "blocked", "task list reset caused TaskUpdate failure"),
        load_bearing=0,
    )
    state = _state()
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=10.0, active_task="P", judge=judge)
    assert blocked is False and steering == ""
    assert state["breaker_armed"] is False


def test_arm_judge_forces_verdict0_when_load_bearing_false():
    def bad_judge(system, user, schema):
        return {
            "verdict": 1,
            "steering": "blocked",
            "claim": "speculative aside",
            "load_bearing": 0,
        }

    verdict, steering, claim = gb.arm_judge("segment", judge=bad_judge)
    assert verdict == 0 and steering == "" and claim == ""


def test_arm_judge_rejects_path_hypothesis_when_read_is_imminent():
    claim = "The acceptance logic lives in benchmark/summarize.py"
    segment = (
        "assistant: I'll inspect summarize.py next.\n"
        '[tool_use name=Read]{"file_path":"benchmark/summarize.py"}\n'
    )
    input_data = {"tool_name": "Read", "tool_input": {"file_path": "benchmark/summarize.py"}}

    def bad_judge(system, user, schema):
        return {
            "verdict": 1,
            "steering": "read summarize.py before asserting",
            "claim": claim,
            "load_bearing": 1,
        }

    verdict, steering, out_claim = gb.arm_judge(segment, judge=bad_judge, input_data=input_data)
    assert verdict == 0 and steering == "" and out_claim == ""


def test_arm_judge_rejects_harness_self_referential_claim():
    def bad_judge(system, user, schema):
        return {
            "verdict": 1,
            "steering": "fetch authoritative unifable documentation",
            "claim": (
                "the run is waived under quick/LIGHT mode or a provisional lift exists, "
                "so edits are allowed despite unresolved spec tasks"
            ),
            "load_bearing": 1,
        }

    verdict, steering, claim = gb.arm_judge("segment", judge=bad_judge)
    assert verdict == 0 and steering == "" and claim == ""


def test_disarm_judge_releases_harness_self_referential_claim():
    def bad_judge(system, user, schema):
        return {
            "grounded": 0,
            "needed": "fetch unifable CLI help",
            "load_bearing": 1,
            "provisional_release": 0,
            "lift_reason": "",
            "lift_scope": "",
        }

    verdict = gb.disarm_judge(
        "LIGHT mode waives the evidence gate for this session",
        "transcript",
        judge=bad_judge,
    )
    assert verdict.grounded is True and verdict.needed == ""


def test_is_harness_self_referential_detects_gate_waiver_claims():
    assert gb.is_harness_self_referential("quick/LIGHT mode waives the spec gate")
    assert gb.is_harness_self_referential("a provisional lift exists for this run")
    assert not gb.is_harness_self_referential("LinkedIn returns contentHtml in the response")


def test_is_task_board_status_claim_detects_validated_narration():
    assert gb.is_task_board_status_claim("T7 already flipped to [OK] this cycle")
    assert gb.is_task_board_status_claim("skip T9 because T7 is validated")
    assert gb.is_task_board_status_claim("breaker: OPEN (all tasks validated)")
    assert not gb.is_task_board_status_claim("fix validation logic in spec.py")


def test_arm_judge_rejects_task_board_status_claim():
    def bad_judge(system, user, schema):
        return {
            "verdict": 1,
            "steering": "read the authoritative spec state",
            "claim": "T7 already flipped to [OK] this cycle",
            "load_bearing": 1,
        }

    board = (
        f"{gb._SPEC_BOARD_BEGIN}\ngoal: ship\n  [OK] T7 (req) version probe\nbreaker: CLOSED (1 left: T9)\n{gb._SPEC_BOARD_END}"
    )
    verdict, steering, claim = gb.arm_judge(f"transcript\n\n{board}", judge=bad_judge)
    assert verdict == 0 and steering == "" and claim == ""


def test_arm_judge_no_arm_when_board_confirms_validated():
    def bad_judge(system, user, schema):
        return {
            "verdict": 1,
            "steering": "should not appear",
            "claim": "T7 is validated and complete",
            "load_bearing": 1,
        }

    board = (
        f"{gb._SPEC_BOARD_BEGIN}\ngoal: ship\n  [OK] T7 (req) version probe\nbreaker: CLOSED (1 left: T9)\n{gb._SPEC_BOARD_END}"
    )
    verdict, steering, claim = gb.arm_judge(f"assistant said T7 done\n\n{board}", judge=bad_judge)
    assert verdict == 0 and steering == "" and claim == ""


def test_disarm_judge_releases_task_board_status_claim():
    board = (
        f"{gb._SPEC_BOARD_BEGIN}\ngoal: ship\n  [XX] T7 (req) version probe\nbreaker: CLOSED (1 left: T7)\n{gb._SPEC_BOARD_END}"
    )

    def bad_judge(system, user, schema):
        return {
            "grounded": 0,
            "needed": "read spec.json",
            "load_bearing": 1,
            "provisional_release": 0,
            "lift_reason": "",
            "lift_scope": "",
        }

    verdict = gb.disarm_judge(
        "T7 already flipped to [OK] this cycle",
        f"transcript\n\n{board}",
        judge=bad_judge,
    )
    assert verdict.grounded is True and verdict.needed == ""


def test_judge_transcript_includes_spec_board(tmp_path, monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "host transcript")
    spec_path_root = tmp_path / "specs"
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))

    def fake_board(input_data):
        return f"{gb._SPEC_BOARD_BEGIN}\ngoal: g\n  [OK] T7 (req) done\nbreaker: OPEN\n{gb._SPEC_BOARD_END}"

    monkeypatch.setattr("breaker_runtime._spec_board_block", fake_board)
    seg = gb.judge_transcript({"session_id": "S", "cwd": str(tmp_path)}, [])
    assert "host transcript" in seg
    assert gb._SPEC_BOARD_BEGIN in seg
    assert "[OK] T7" in seg


def test_disarm_judge_releases_when_not_load_bearing():
    def release_judge(system, user, schema):
        return {
            "grounded": 0,
            "needed": "should be ignored",
            "load_bearing": 0,
            "provisional_release": 0,
            "lift_reason": "",
            "lift_scope": "",
        }

    verdict = gb.disarm_judge("speculative host error", "transcript", judge=release_judge)
    assert verdict.grounded is True and verdict.needed == ""


def test_pre_tool_disarms_when_release_judge_says_not_load_bearing(monkeypatch):
    monkeypatch.setattr(
        gb,
        "transcript_segment",
        lambda d, **k: "assistant: I retract that claim; it is not load-bearing for this edit.",
    )
    state = _state()
    gb.arm(state, gb.breaker_key("S", "P"), 0.0, "blocked", "TaskUpdate failed due to plugin reload")
    judge = RoutingJudge(grounded=0, release_load_bearing=0)
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    assert blocked is False and state["breaker_armed"] is False
    assert judge.disarm_calls == 1


def test_empty_segment_is_not_a_violation():
    judge = FakeJudge([(1, "should not be used")])
    verdict, steering = gb.judge_segment("", judge=judge)
    assert verdict == 0 and steering == "" and judge.calls == 0


def test_verdict0_forces_empty_steering_even_if_model_returns_text():
    judge = FakeJudge([(0, "stray text that should be dropped")])
    verdict, steering = gb.judge_segment("seg", judge=judge)
    assert verdict == 0 and steering == ""


def test_judge_transcript_includes_breaker_events(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "host transcript")
    events = [{"kind": "DISARM", "ts": "2026-01-01T00:00:00+00:00", "claim": "old claim", "grounded": True}]
    seg = gb.judge_transcript(_pre("Read"), events, fresh_tool="fresh output")
    assert "unifable_breaker" in seg
    assert "event=DISARM" in seg
    assert "host transcript" in seg
    assert "fresh output" in seg


def test_render_events_and_adjudicated_claims():
    events = [
        {"kind": "ARM", "claim": "x", "steering": "read y"},
        {"kind": "DISARM", "claim": "x", "grounded": True},
        {"kind": "FAIL_OPEN", "claim": "z", "block_count": 3},
    ]
    rendered = render_events(events)
    assert "event=ARM" in rendered and "event=DISARM" in rendered
    assert adjudicated_claims(events) == ["x", "z"]


def test_transcript_segment_reads_path(tmp_path):
    import json

    f = tmp_path / "t.jsonl"
    f.write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "the fix is X"}]}})
        + "\n"
        + json.dumps(
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ran probe -> 0 ok"}]}}
        )
        + "\n",
        encoding="utf-8",
    )
    seg = gb.transcript_segment({"transcript_path": str(f)})
    assert "the fix is X" in seg and "ran probe" in seg
    assert '<record line="000001" type="assistant" role="assistant"' in seg
    assert "[tool_result]" in seg


def test_transcript_segment_preserves_full_tool_call_and_result(tmp_path):
    import json

    f = tmp_path / "t.jsonl"
    tool_input_tail = "INPUT_TAIL_" + ("x" * 700)
    tool_result_tail = "RESULT_TAIL_" + ("y" * 900)
    f.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "echo " + tool_input_tail},
                        }
                    ],
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": "output " + tool_result_tail}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    seg = gb.transcript_segment({"transcript_path": str(f)})
    assert "@@tool Bash" in seg
    assert "[tool_result]" in seg
    assert tool_input_tail in seg
    assert tool_result_tail in seg


def test_transcript_segment_tails_by_token_budget(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text("old0 old1 old2 keep3 keep4 keep5", encoding="utf-8")
    seg = gb.transcript_segment({"transcript_path": str(f)}, max_tokens=5)
    assert "old0" not in seg
    assert "keep5" in seg


def test_transcript_segment_missing_is_empty():
    assert gb.transcript_segment({"transcript_path": "/no/such.jsonl", "session_id": "z", "cwd": "/x"}) == ""


def test_disarm_adds_event_preventing_re_arm(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "blocked", "claim to disarm"), grounded=1)
    state = _state()
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    gb.evaluate_post_tool_release(_pre("Read"), state, fresh_tool="[tool_result name=Read]\nok", judge=judge)
    assert state["breaker_armed"] is False
    assert "claim to disarm" in adjudicated_claims(state["events"])


def test_safety_cap_adds_fail_open_event(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    monkeypatch.setenv("UNIFABLE_BREAKER_MAX_BLOCKS", "3")
    judge = RoutingJudge(arm=(1, "blocked", "uncapped claim"), grounded=0)
    state = _state()
    b1, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    b2, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    b3, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=2.0, active_task="P", judge=judge)
    assert b1 is True and b2 is True and b3 is False
    assert "uncapped claim" in adjudicated_claims(state["events"])
    assert any(e.get("kind") == "FAIL_OPEN" for e in state["events"])


def test_adjudicated_events_prevent_re_arm(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    state = _state()
    append_event(state, "DISARM", claim="my claim", grounded=True)

    called_system_prompt = []
    called_user = []

    def recording_judge(system, user, schema):
        called_system_prompt.append(system)
        called_user.append(user)
        return {
            "verdict": 1,
            "steering": "blocked",
            "claim": "my claim",
            "load_bearing": 1,
        }

    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=100.0, active_task="P", judge=recording_judge)
    assert blocked is False
    assert state.get("breaker_armed") is False
    assert len(called_system_prompt) == 1
    # The adjudicated-claims list now rides the END of the USER message (prompt-cache
    # prefix stability); the system prompt stays a byte-identical cacheable constant.
    assert called_system_prompt[0] == gb._JUDGE_SYSTEM
    assert "do NOT flag any of the following claims" in called_user[0]
    assert "- my claim" in called_user[0]


def test_adjudication_survives_event_log_overflow(monkeypatch):
    """RC2: claim_already_adjudicated must not lose a resolved claim when the
    bounded `events` log (MAX_EVENTS) ages out the DISARM event. The durable
    `breaker_adjudicated_claims` list carries the guard so a long session (or a
    /compact that keeps the same session+cwd) cannot re-arm a resolved claim."""
    from breaker_state import MAX_EVENTS, append_event, claim_already_adjudicated, default_breaker

    state = default_breaker()
    # Disarm a claim, then flood the log past MAX_EVENTS so the DISARM ages out.
    append_event(state, "DISARM", claim="the parser bug is X", grounded=True)
    from breaker_runtime import _apply_release  # disarm path records the durable claim
    # Simulate the durable record the disarm path writes.
    state.setdefault("breaker_adjudicated_claims", [])
    if "the parser bug is X" not in state["breaker_adjudicated_claims"]:
        state["breaker_adjudicated_claims"].append("the parser bug is X")
    for i in range(MAX_EVENTS + 5):
        append_event(state, "NEEDED", claim=f"noise {i}", needed="x")
    # The DISARM event is gone from the trimmed log...
    assert not any(e.get("kind") == "DISARM" for e in state["events"])
    # ...but the durable list still suppresses re-arm.
    assert claim_already_adjudicated(
        "the parser bug is X", state["events"], extra_claims=state["breaker_adjudicated_claims"]
    )


def test_paraphrased_claim_blocked_by_token_overlap():
    """RC2: a post-compact judge re-wording an already-resolved claim must not
    re-arm. Substring containment misses paraphrases; token-overlap catches them."""
    from breaker_state import claim_already_adjudicated

    events = [{"kind": "DISARM", "claim": "the daemon mis-maps the Account ARR Tier option field", "grounded": True}]
    # A reworded restatement of the SAME resolved claim (no substring containment).
    paraphrase = "Account ARR Tier option field is mis-mapped by the daemon causing the 400"
    assert claim_already_adjudicated(paraphrase, events)
    # An unrelated claim is NOT suppressed.
    assert not claim_already_adjudicated("the summary was auto-rewritten on create", events)


def test_unresolved_arm_persists_across_compact(monkeypatch):
    """RC2 counterpart: a genuinely-armed UNRESOLVED claim must survive a compact
    (same session+cwd). Nothing in the breaker disarms on compact; only a fresh
    grounded judge verdict clears it. This pins that an armed claim is NOT dropped
    merely because the transcript was compacted."""
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    monkeypatch.setattr("breaker_runtime.latest_user_prompt_fingerprint", lambda p: "same-task", raising=False)
    judge = RoutingJudge(arm=(1, "blocked: prove it", "claim still unproven"), grounded=0)
    state = _state()
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit", session="S"), state, now=0.0, active_task="", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    # Simulate compact: same session, active_task empty, SAME task fingerprint, and
    # the release judge still says not grounded. The arm must remain.
    blocked2, _, _ = gb.evaluate_pre_tool(_pre("Edit", session="S"), state, now=5.0, active_task="", judge=judge)
    assert blocked2 is True and state["breaker_armed"] is True


def test_provisional_lift_allows_edit_without_full_ground(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "read baselines cited")
    judge = RoutingJudge(
        arm=(1, "blocked", "unproven quality claim"),
        grounded=0,
        provisional_release=1,
        lift_reason="Baselines read; configure the experiment.",
        lift_scope="Edit prompt-adaptation config only.",
    )
    state = _state()
    gb.evaluate_pre_tool(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    blocked, _, notify = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    assert blocked is False
    assert state["breaker_provisional"] is True
    assert state["breaker_armed"] is False
    assert state["breaker_block_count"] == 0
    assert "temporary lift" in notify.lower()
    assert any(e.get("kind") == "LIFT" for e in state["events"])


def test_provisional_monitor_reinstates_on_egregious_drift(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    from breaker_state import lift_provisional

    state = _state()
    lift_provisional(
        state,
        "claim X",
        "reason",
        "edit config only",
        "notify",
    )
    state["breaker_key"] = gb.breaker_key("S", "P")
    judge = RoutingJudge(
        grounded=0,
        monitor_drift_level=2,
        monitor_feedback="Stop unrelated refactors.",
    )
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    assert blocked is True
    assert state["breaker_armed"] is True
    assert state["breaker_provisional"] is False
    assert "Stop unrelated refactors" in steering
    assert any(e.get("kind") == "REINSTATE" for e in state["events"])


def test_provisional_monitor_hints_on_minor_drift(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    from breaker_state import lift_provisional

    state = _state()
    lift_provisional(state, "claim X", "reason", "edit config only", "")
    state["breaker_key"] = gb.breaker_key("S", "P")
    judge = RoutingJudge(
        grounded=0,
        monitor_drift_level=1,
        monitor_feedback="Consider citing the Chromium IV from source.",
    )
    blocked, _, notify = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    assert blocked is False
    assert state["breaker_provisional"] is True
    assert "hint:" in notify.lower()
    assert "Chromium IV" in notify
    assert any(e.get("kind") == "SCOPE_HINT" for e in state["events"])


def test_provisional_disarm_on_pre_tool_after_grounding(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "chromium source cited")
    from breaker_state import lift_provisional

    state = _state()
    lift_provisional(state, "claim X", "reason", "scope", "")
    state["breaker_key"] = gb.breaker_key("S", "P")
    judge = RoutingJudge(grounded=1, monitor_drift_level=2, monitor_feedback="should not run")
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    assert blocked is False
    assert state["breaker_provisional"] is False
    assert state["breaker_armed"] is False
    assert judge.disarm_calls == 1
    assert judge.monitor_calls == 0


def test_provisional_monitor_allows_on_track_edit(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    from breaker_state import lift_provisional

    state = _state()
    lift_provisional(state, "claim X", "reason", "edit config only", "")
    state["breaker_key"] = gb.breaker_key("S", "P")
    judge = RoutingJudge(grounded=0, monitor_drift_level=0)
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    assert blocked is False
    assert state["breaker_provisional"] is True
    assert judge.monitor_calls == 1
    assert judge.disarm_calls == 1


def test_full_disarm_clears_provisional(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    from breaker_state import lift_provisional

    state = _state()
    lift_provisional(state, "claim X", "reason", "scope", "notify")
    state["breaker_key"] = gb.breaker_key("S", "P")
    judge = RoutingJudge(grounded=1)
    disarmed, _, msg = gb.evaluate_post_tool_release(_pre("Read"), state, fresh_tool="[tool_result]\nok", judge=judge)
    assert disarmed is True
    assert state["breaker_provisional"] is False
    assert state["breaker_armed"] is False
    assert "claim grounded" in msg.lower()


def test_loaded_skill_names_parses_skill_tool_use():
    seg = (
        '<record line="000001" type="assistant" role="assistant">\n'
        '@@tool Skill line=000001\n{\n  "command": "release"\n}\n'
        'stats: input_sha256=abc\n</record>\n'
        '<record line="000002" type="user" role="user">\n'
        "[tool_result]\nSuccessfully loaded skill\n</record>"
    )
    assert "release" in gb.loaded_skill_names(seg)
    assert gb.loaded_skill_names("no skill loaded here") == set()


def test_loaded_skill_names_parses_legacy_skill_tool_use():
    seg = (
        '<record line="000001" type="assistant" role="assistant">\n'
        '[tool_use name=Skill]\n{"command": "release"}\n</record>\n'
    )
    assert "release" in gb.loaded_skill_names(seg)


def test_claim_describes_loaded_skill_requires_skill_context():
    seg = '@@tool Skill\n{\n  "command": "release"\n}\nstats: input_sha256=abc'
    assert gb.claim_describes_loaded_skill("the release skill handles X", seg) is True
    assert gb.claim_describes_loaded_skill("use skill: release to ship", seg) is True
    # bare skill-name word with no skill context is NOT suppressed (repo claim)
    assert gb.claim_describes_loaded_skill("the release workflow in ci.yml runs publish", seg) is False
    # no skill loaded -> never suppressed
    assert gb.claim_describes_loaded_skill("the release skill handles X", "no load") is False


def test_arm_judge_does_not_arm_on_just_loaded_skill_behavior():
    segment = (
        '<record line="000001" type="assistant" role="assistant">\n'
        '@@tool Skill\n{\n  "command": "release"\n}\nstats: input_sha256=abc\n</record>\n'
        '<record line="000002" type="user" role="user">\n'
        "[tool_result]\nSuccessfully loaded skill\n</record>"
    )

    def judge(system, user, schema):
        return {
            "verdict": 1,
            "steering": "ground the claim that the release skill handles the release tail",
            "claim": (
                "the release skill handles the full release tail end-to-end (commit, version bump, push, npm publish, verify)"
            ),
            "load_bearing": 1,
        }

    verdict, steering, claim = gb.arm_judge(segment, judge=judge)
    assert verdict == 0 and steering == "" and claim == ""


def test_arm_judge_still_arms_on_repo_claim_despite_loaded_skill():
    segment = '@@tool Skill\n{"command": "release"}\nstats: x\n[tool_result]\nok'

    def judge(system, user, schema):
        return {
            "verdict": 1,
            "steering": "read ci.yml before asserting publish behavior",
            "claim": "the release workflow in ci.yml runs npm publish on tag push",
            "load_bearing": 1,
        }

    verdict, steering, claim = gb.arm_judge(segment, judge=judge)
    assert verdict == 1 and claim


def test_disarm_judge_releases_claim_about_loaded_skill():
    segment = '@@tool Skill\n{"command": "release"}\nstats: x\n[tool_result]\nSuccessfully loaded skill'

    def judge(system, user, schema):
        return {
            "grounded": 0,
            "needed": "read the release skill source",
            "load_bearing": 1,
            "provisional_release": 0,
            "lift_reason": "",
            "lift_scope": "",
        }

    verdict = gb.disarm_judge(
        "the release skill handles the full release tail end-to-end",
        segment,
        judge=judge,
    )
    assert verdict.grounded is True and verdict.needed == ""


# ---------------------------------------------------------------------------
# Auto-grounding lane (async background verification): dispatch + poll + disarm
# ---------------------------------------------------------------------------


class VerifyArmJudge:
    """Arm judge that decomposes the claim into background verify_tasks."""

    def __init__(self, tasks):
        self.tasks = tasks
        self.calls = 0

    def __call__(self, system, user, schema):
        self.calls += 1
        return {
            "verdict": 1,
            "steering": "blocked: prove the release before pushing",
            "claim": "release can proceed without verification",
            "load_bearing": 1,
            "verify_tasks": self.tasks,
        }


_VTASKS = [
    {"subclaim": "tests pass", "command": "just test-all"},
    {"subclaim": "version consistent", "command": "just version-check"},
]


def _arm_with_verify(monkeypatch, dispatched):
    """Arm the breaker on a claim with sanctioned verify_tasks; record dispatch."""
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "model: release is ready, pushing now")
    monkeypatch.setattr("verify_lane.sanction_tasks", lambda raw, cwd: list(raw))

    def _fake_dispatch(input_data, claim, tasks, cwd):
        dispatched["claim"] = claim
        dispatched["tasks"] = list(tasks)
        return "verifykey1"

    monkeypatch.setattr("verify_lane.dispatch_verification", _fake_dispatch)
    state = _state()
    judge = VerifyArmJudge(_VTASKS)
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=100.0, active_task="P", judge=judge)
    return state, blocked, steering


def test_arm_dispatches_background_verification(monkeypatch):
    dispatched = {}
    state, blocked, steering = _arm_with_verify(monkeypatch, dispatched)
    assert blocked is True
    assert state["breaker_armed"] is True
    assert state["breaker_verify_key"] == "verifykey1"
    assert [t["command"] for t in state["breaker_verify_tasks"]] == [
        "just test-all",
        "just version-check",
    ]
    assert all(t["status"] == "pending" for t in state["breaker_verify_tasks"])
    assert dispatched["tasks"] == _VTASKS
    assert "dispatched to the background" in steering.lower()


def test_does_not_dispatch_when_no_verify_tasks(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "model: the cause is obviously Y")
    judge = FakeJudge([(1, "you claimed Y without proof; blocked")])
    state = _state()
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=100.0, active_task="P", judge=judge)
    assert blocked is True
    assert state["breaker_verify_key"] == ""
    assert state["breaker_verify_tasks"] == []


def test_poll_confirms_all_and_disarms_with_digest(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    state, _, _ = _arm_with_verify(monkeypatch, {})
    results = {
        "just test-all": {"exit": 0, "tail": "passed"},
        "just version-check": {"exit": 0, "tail": "ok"},
    }
    monkeypatch.setattr("verify_lane.read_verification_results", lambda d, k: results)
    # A read tool while armed: armed branch polls, all pass -> disarm with digest.
    blocked, steering, notify = gb.evaluate_pre_tool(_pre("Read"), state, now=120.0, active_task="P", judge=None)
    assert blocked is False
    assert state["breaker_armed"] is False
    assert state["breaker_verify_key"] == ""
    assert "grounded automatically" in notify.lower()
    assert "tests pass" in notify and "version consistent" in notify


def test_poll_partial_then_full_confirm(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    state, _, _ = _arm_with_verify(monkeypatch, {})
    # First poll: only one result back -> stays armed, one confirmation surfaced.
    monkeypatch.setattr(
        "verify_lane.read_verification_results",
        lambda d, k: {"just test-all": {"exit": 0, "tail": "passed"}},
    )
    blocked, _, notify = gb.evaluate_pre_tool(_pre("Read"), state, now=120.0, active_task="P", judge=None)
    assert blocked is False and state["breaker_armed"] is True
    assert "confirmed: tests pass" in notify.lower()
    assert state["breaker_verify_tasks"][0]["status"] == "passed"
    assert state["breaker_verify_tasks"][1]["status"] == "pending"
    # Second poll: the rest pass -> disarm.
    monkeypatch.setattr(
        "verify_lane.read_verification_results",
        lambda d, k: {
            "just test-all": {"exit": 0, "tail": "passed"},
            "just version-check": {"exit": 0, "tail": "ok"},
        },
    )
    blocked, _, notify = gb.evaluate_pre_tool(_pre("Read"), state, now=125.0, active_task="P", judge=None)
    assert state["breaker_armed"] is False
    assert "grounded automatically" in notify.lower()


def test_poll_failure_keeps_armed_and_reverts_to_normal(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    state, _, _ = _arm_with_verify(monkeypatch, {})
    monkeypatch.setattr(
        "verify_lane.read_verification_results",
        lambda d, k: {
            "just test-all": {"exit": 1, "tail": "1 failed"},
            "just version-check": {"exit": 0, "tail": "ok"},
        },
    )
    blocked, _, notify = gb.evaluate_pre_tool(_pre("Read"), state, now=120.0, active_task="P", judge=None)
    assert state["breaker_armed"] is True  # not all passed -> stays armed
    assert state["breaker_verify_key"] == ""  # auto-verify handed back to model
    assert "verification failed" in notify.lower()


def test_block_cap_exempt_while_verifying(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    state, _, _ = _arm_with_verify(monkeypatch, {})
    monkeypatch.setattr("verify_lane.read_verification_results", lambda d, k: {})  # nothing back yet
    # Far more mutation attempts than BREAKER_MAX_BLOCKS: must never fail open while
    # the background verification is still in flight (within its window).
    for i in range(gb.max_blocks() + 4):
        blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=120.0 + i, active_task="P", judge=None)
        assert blocked is True
        assert state["breaker_armed"] is True
    assert int(state["breaker_block_count"]) == 0


def test_post_tool_release_polls_and_disarms(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "transcript")
    state, _, _ = _arm_with_verify(monkeypatch, {})
    monkeypatch.setattr(
        "verify_lane.read_verification_results",
        lambda d, k: {
            "just test-all": {"exit": 0, "tail": "passed"},
            "just version-check": {"exit": 0, "tail": "ok"},
        },
    )
    grounded, needed, message = gb.evaluate_post_tool_release(
        _pre("Read"), state, fresh_tool="[tool_result name=Read]\nok", judge=None
    )
    assert grounded is True and needed == ""
    assert state["breaker_armed"] is False
    assert "grounded automatically" in message.lower()


def test_dispatch_failure_falls_back_to_normal_arm(monkeypatch):
    monkeypatch.setattr("breaker_runtime.transcript_segment", lambda d, **k: "model: release is ready")
    monkeypatch.setattr("verify_lane.sanction_tasks", lambda raw, cwd: list(raw))

    def _boom(*a, **k):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr("verify_lane.dispatch_verification", _boom)
    state = _state()
    judge = VerifyArmJudge(_VTASKS)
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=100.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    assert state["breaker_verify_key"] == ""  # no auto-verify; ordinary block


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
