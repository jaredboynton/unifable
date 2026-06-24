#!/usr/bin/env python3
"""Logic tests for the overconfidence / groundedness breaker (groundedness.py).

Each test maps to a requirement of the breaker:
  R1  block ONLY Write/Edit/Bash; never WebSearch/Read/WebFetch/Grep/Glob
  R2  arm (strict judge, verdict 1) and disarm (separate claim-bound release judge)
  R5  judge question = "did the model say something confidently w/o backing it up"
  R6  verdict 1 -> steering prompt returned + mutation blocked until evidence read
  R7  verdict 0 -> no steering, model sees nothing (no block)
  R8  release via PostToolUse after Read/WebFetch with fresh tool output
  R9  arm judge debounced: <=1 call / 15s per session+user-prompt key
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
                    "grounded": 1, "needed": "", "load_bearing": lb,
                    "provisional_release": 0, "lift_reason": "", "lift_scope": "",
                }
            if self.provisional_release:
                return {
                    "grounded": 0, "needed": "", "load_bearing": lb,
                    "provisional_release": 1,
                    "lift_reason": self.lift_reason,
                    "lift_scope": self.lift_scope,
                }
            return {
                "grounded": 0, "needed": self.needed, "load_bearing": lb,
                "provisional_release": 0, "lift_reason": "", "lift_scope": "",
            }
        self.arm_calls += 1
        v, s, c = self.arm_ret
        return {"verdict": v, "steering": s, "claim": c, "load_bearing": self.load_bearing if v == 1 else 0}


def _pre(tool, session="S"):
    return {"tool_name": tool, "session_id": session, "cwd": "/repo"}


def _state():
    return default_breaker()


def test_mutation_set_is_exactly_writes_edits_bash():
    for t in ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "Bash"):
        assert gb.is_mutation_tool(t), t
    for t in ("WebSearch", "Read", "WebFetch", "Grep", "Glob", "Task", "TodoWrite"):
        assert not gb.is_mutation_tool(t), t


def test_release_tools_include_reads_and_fetch():
    for t in ("Read", "WebFetch", "WebSearch", "Grep", "Glob", "NotebookRead"):
        assert gb.is_release_tool(t), t


def test_read_and_websearch_never_blocked_even_when_armed(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "model: definitely the cause is Y")
    judge = FakeJudge([(1, "you claimed Y with no proof; mutation blocked")])
    state = _state()
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=100.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Read"), state, now=101.0, active_task="P", judge=judge)
    assert blocked is False and steering == ""
    blocked, _, _ = gb.evaluate_pre_tool(_pre("WebSearch"), state, now=102.0, active_task="P", judge=judge)
    assert blocked is False


def test_verdict1_blocks_mutation_and_returns_steering(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "model: the fix is obviously Z")
    judge = FakeJudge([(1, "You asserted Z without reading the source. Write/Edit/Bash blocked until you cite evidence.")])
    state = _state()
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=10.0, active_task="P", judge=judge)
    assert blocked is True
    assert "blocked" in steering.lower() and steering != ""


def test_verdict0_no_block_no_steering(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "model: I read foo.py:10, it does X; here is the edit")
    judge = FakeJudge([(0, "")])
    state = _state()
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Write"), state, now=10.0, active_task="P", judge=judge)
    assert blocked is False and steering == ""
    assert state["breaker_armed"] is False


def test_arms_then_disarms_via_post_tool_release(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "unproven; blocked", "the cause is Y"), grounded=1)
    state = _state()
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    assert judge.arm_calls == 1 and judge.disarm_calls == 0
    grounded, needed, message = gb.evaluate_post_tool_release(
        _pre("Read"), state, fresh_tool="[tool_result name=Read]\nevidence", judge=judge
    )
    assert grounded is True and needed == "" and "breaker open" in message.lower()
    assert state["breaker_armed"] is False
    assert judge.disarm_calls == 1


def test_armed_stays_blocked_on_mutation_without_post_tool_release(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "blocked", "claim X"), grounded=0, release_load_bearing=1)
    state = _state()
    gb.evaluate_pre_tool(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    assert state["breaker_armed"] is True
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=99.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    assert judge.disarm_calls == 1


def test_post_tool_release_not_grounded_stays_armed(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(
        arm=(1, "blocked", "claim X"), grounded=0, needed="still missing: read codex_judge.py:54 and cite MODEL"
    )
    state = _state()
    gb.evaluate_pre_tool(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    grounded, needed, message = gb.evaluate_post_tool_release(
        _pre("Read"), state, fresh_tool="[tool_result name=Read]\nwrong file", judge=judge
    )
    assert grounded is False and state["breaker_armed"] is True
    assert judge.disarm_calls == 1
    assert needed == "still missing: read codex_judge.py:54 and cite MODEL"
    assert "still armed" in message.lower()
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    assert blocked is True
    assert steering == needed


def test_arm_judge_debounced_at_most_once_per_15s_per_key(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")
    judge = RoutingJudge(arm=(0, "", ""))
    state = _state()
    gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    gb.evaluate_pre_tool(_pre("Bash"), state, now=5.0, active_task="P", judge=judge)
    gb.evaluate_pre_tool(_pre("Bash"), state, now=14.9, active_task="P", judge=judge)
    assert judge.arm_calls == 1
    gb.evaluate_pre_tool(_pre("Bash"), state, now=15.0, active_task="P", judge=judge)
    assert judge.arm_calls == 2


def test_safety_cap_fails_open_after_max_blocks(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    monkeypatch.setenv("UNIFABLE_BREAKER_MAX_BLOCKS", "3")
    judge = RoutingJudge(arm=(1, "blocked", "claim X"), grounded=0)
    state = _state()
    b1, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    b2, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    b3, _, _ = gb.evaluate_pre_tool(_pre("Edit"), state, now=2.0, active_task="P", judge=judge)
    assert b1 is True and b2 is True
    assert b3 is False and state["breaker_armed"] is False


def test_new_user_prompt_drops_stale_arm(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(0, "", ""))
    state = _state()
    gb.arm(state, gb.breaker_key("S", "P1"), 0.0, "blocked", "claim X")
    assert state["breaker_armed"] is True
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Edit", session="S"), state, now=1.0, active_task="P2", judge=judge)
    assert blocked is False and state["breaker_armed"] is False
    assert judge.arm_calls == 1
    assert any(e.get("kind") == "STALE_ARM_DROPPED" for e in state["events"])


def test_debounce_key_is_session_plus_user_prompt(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")
    judge = FakeJudge([(1, "blocked")])
    state = _state()
    gb.evaluate_pre_tool(_pre("Bash", session="S"), state, now=0.0, active_task="P1", judge=judge)
    gb.evaluate_pre_tool(_pre("Bash", session="S"), state, now=1.0, active_task="P2", judge=judge)
    assert judge.calls == 2
    gb.evaluate_pre_tool(_pre("Bash", session="S2"), state, now=1.5, active_task="P2", judge=judge)
    assert judge.calls == 3


def test_judge_exception_fails_open(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")

    def boom(system, user, schema):
        raise RuntimeError("realtime down")

    state = _state()
    blocked, steering, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=boom)
    assert blocked is False and steering == ""


def test_judge_system_prompt_asks_the_confidence_question():
    seg = "model said something"
    judge = FakeJudge([(1, "x")])
    gb.judge_segment(seg, judge=judge)
    sysp = judge.systems[0].lower()
    assert "ungrounded" in sysp and "confident" in sysp
    assert "load_bearing" in sysp or "load-bearing" in sysp
    assert "restricted to read-only" in sysp
    assert "read, websearch, webfetch, grep, glob" in sysp
    assert "trace.sh" in sysp or "whitelisted" in sysp
    assert "explore skill" in sysp


def test_judge_prompts_forbid_steering_toward_blocked_commands():
    arm_sysp = gb._JUDGE_SYSTEM.lower()
    disarm_sysp = gb._DISARM_SYSTEM.lower()
    steering_desc = gb._JUDGE_SCHEMA["properties"]["steering"]["description"].lower()
    needed_desc = gb._DISARM_SCHEMA["properties"]["needed"]["description"].lower()
    assert "never" in steering_desc and ("blocked" in steering_desc or "blocks" in steering_desc)
    assert "trace.sh" in steering_desc or "whitelisted" in steering_desc
    assert "explore skill" in steering_desc
    assert "never" in arm_sysp or "never steer" in arm_sysp
    assert "blocked" in disarm_sysp or "blocked scorer" in disarm_sysp
    assert "retract" in disarm_sysp or "superseded" in disarm_sysp
    assert "never" in needed_desc and "blocked" in needed_desc


def test_disarm_prompt_accepts_read_derived_scoring_math():
    sysp = gb._DISARM_SYSTEM.lower()
    assert "deriving" in sysp or "formulas" in sysp
    assert "read" in sysp
    assert "re-running" in sysp or "re-run" in sysp or "blocked scorer" in sysp


def test_arm_prompt_does_not_arm_on_retraction_or_aside():
    sysp = gb._JUDGE_SYSTEM.lower()
    assert "retract" in sysp
    assert "load-bearing" in sysp or "load_bearing" in sysp
    assert "work currently in progress" in sysp or "current work" in sysp


def test_arm_prompt_skips_host_error_speculation_while_editing_repo():
    sysp = gb._JUDGE_SYSTEM.lower()
    assert "taskupdate" in sysp or "task" in sysp
    assert "not load-bearing" in sysp or "load_bearing=0" in sysp


def test_completion_stall_circuit_breaker_backstop_releases():
    """The completion breaker is a bounded circuit breaker: on a stalled
    (no-net-progress) run of blocks it trips and releases Stop instead of
    trapping the session. Covers the unsat-budget stall-release backstop
    (stall / backstop / circuit)."""
    import verify_state as vs

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
            "user: update spec.py session keying\n"
            "assistant: TaskUpdate failed because the task list reset after plugin reload."
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


def test_arm_prompt_forbids_harness_self_reference():
    sysp = gb._JUDGE_SYSTEM.lower()
    assert "self-referential" in sysp or "self reference" in sysp
    assert "light" in sysp or "waiver" in sysp
    lb_desc = gb._JUDGE_SCHEMA["properties"]["load_bearing"]["description"].lower()
    assert "unifable" in lb_desc or "harness" in lb_desc


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
        f"{gb._SPEC_BOARD_BEGIN}\n"
        "goal: ship\n  [OK] T7 (req) version probe\nbreaker: CLOSED (1 left: T9)\n"
        f"{gb._SPEC_BOARD_END}"
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
        f"{gb._SPEC_BOARD_BEGIN}\n"
        "goal: ship\n  [OK] T7 (req) version probe\nbreaker: CLOSED (1 left: T9)\n"
        f"{gb._SPEC_BOARD_END}"
    )
    verdict, steering, claim = gb.arm_judge(f"assistant said T7 done\n\n{board}", judge=bad_judge)
    assert verdict == 0 and steering == "" and claim == ""


def test_disarm_judge_releases_task_board_status_claim():
    board = (
        f"{gb._SPEC_BOARD_BEGIN}\n"
        "goal: ship\n  [XX] T7 (req) version probe\nbreaker: CLOSED (1 left: T7)\n"
        f"{gb._SPEC_BOARD_END}"
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
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "host transcript")
    spec_path_root = tmp_path / "specs"
    monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))

    def fake_board(input_data):
        return (
            f"{gb._SPEC_BOARD_BEGIN}\n"
            "goal: g\n  [OK] T7 (req) done\nbreaker: OPEN\n"
            f"{gb._SPEC_BOARD_END}"
        )

    monkeypatch.setattr(gb, "_spec_board_block", fake_board)
    seg = gb.judge_transcript({"session_id": "S", "cwd": str(tmp_path)}, [])
    assert "host transcript" in seg
    assert gb._SPEC_BOARD_BEGIN in seg
    assert "[OK] T7" in seg


def test_disarm_judge_releases_when_not_load_bearing():
    def release_judge(system, user, schema):
        return {
            "grounded": 0, "needed": "should be ignored", "load_bearing": 0,
            "provisional_release": 0, "lift_reason": "", "lift_scope": "",
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


def test_disarm_prompt_releases_on_retraction_and_bounded_negative():
    sysp = gb._DISARM_SYSTEM.lower()
    assert "retract" in sysp
    assert "load_bearing" in sysp or "load-bearing" in sysp
    assert "load_bearing=0" in sysp or "not load-bearing" in sysp


def test_arm_prompt_external_claims_allow_docs_prior_art_or_empirical_re():
    sysp = gb._JUDGE_SYSTEM.lower()
    assert "external" in sysp
    assert "reverse" in sysp or "empirical" in sysp
    assert "github" in sysp or "prior art" in sysp or "prior-art" in sysp
    assert "documentation" in sysp or "webfetch" in sysp
    desc = gb._JUDGE_SCHEMA["properties"]["steering"]["description"].lower()
    assert "documentation" in desc
    assert "github" in desc or "prior art" in desc
    assert "dig in" in desc or "start empirical" in desc


def test_arm_prompt_steers_in_repo_version_conventions_to_agents_md():
    desc = gb._JUDGE_SCHEMA["properties"]["steering"]["description"].lower()
    assert "agents.md" in desc or "just version" in desc or "bump_version" in desc
    needed = gb._DISARM_SCHEMA["properties"]["needed"]["description"].lower()
    assert "agents.md" in needed or "just version" in needed or "bump_version" in needed


def test_disarm_prompt_releases_on_prior_art_or_empirical_re():
    sysp = gb._DISARM_SYSTEM.lower()
    assert "reverse" in sysp or "empirical" in sysp
    assert "github" in sysp or "prior" in sysp
    grounded_desc = gb._DISARM_SCHEMA["properties"]["grounded"]["description"].lower()
    assert "reverse" in grounded_desc or "empirical" in grounded_desc
    assert "github" in grounded_desc or "prior" in grounded_desc


def test_empty_segment_is_not_a_violation():
    judge = FakeJudge([(1, "should not be used")])
    verdict, steering = gb.judge_segment("", judge=judge)
    assert verdict == 0 and steering == "" and judge.calls == 0


def test_verdict0_forces_empty_steering_even_if_model_returns_text():
    judge = FakeJudge([(0, "stray text that should be dropped")])
    verdict, steering = gb.judge_segment("seg", judge=judge)
    assert verdict == 0 and steering == ""


def test_judge_transcript_includes_breaker_events(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "host transcript")
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
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                    "content": [{"type": "text", "text": "the fix is X"}]}}) + "\n"
        + json.dumps({"type": "user", "message": {"role": "user",
                      "content": [{"type": "tool_result", "content": "ran probe -> 0 ok"}]}}) + "\n",
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
        json.dumps({
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
        }) + "\n"
        + json.dumps({
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "content": "output " + tool_result_tail}
                ],
            },
        }) + "\n",
        encoding="utf-8",
    )
    seg = gb.transcript_segment({"transcript_path": str(f)})
    assert "[tool_use name=Bash]" in seg
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
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "blocked", "claim to disarm"), grounded=1)
    state = _state()
    blocked, _, _ = gb.evaluate_pre_tool(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    gb.evaluate_post_tool_release(
        _pre("Read"), state, fresh_tool="[tool_result name=Read]\nok", judge=judge
    )
    assert state["breaker_armed"] is False
    assert "claim to disarm" in adjudicated_claims(state["events"])


def test_safety_cap_adds_fail_open_event(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
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
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    state = _state()
    append_event(state, "DISARM", claim="my claim", grounded=True)

    called_system_prompt = []

    def recording_judge(system, user, schema):
        called_system_prompt.append(system)
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
    assert "Do NOT flag any of the following claims" in called_system_prompt[0]
    assert "- my claim" in called_system_prompt[0]


def test_disarm_prompt_mentions_provisional_release():
    sysp = gb._DISARM_SYSTEM.lower()
    assert "provisional" in sysp
    assert "pursuing" in sysp or "verification" in sysp


def test_monitor_prompt_mentions_drift_levels_and_hints():
    sysp = gb._MONITOR_SYSTEM.lower()
    assert "drift_level" in sysp
    assert "user goal" in sysp
    assert "advisory" in sysp or "hint" in sysp
    assert "egregious" in sysp


def test_disarm_prompt_mentions_empirical_validation():
    sysp = gb._DISARM_SYSTEM.lower()
    assert "empirical" in sysp
    assert "user goal" in sysp


def test_provisional_lift_allows_edit_without_full_ground(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "read baselines cited")
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
    assert "provisional lift" in notify.lower()
    assert any(e.get("kind") == "LIFT" for e in state["events"])


def test_provisional_monitor_reinstates_on_egregious_drift(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
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
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
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
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "chromium source cited")
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
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
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
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    from breaker_state import lift_provisional

    state = _state()
    lift_provisional(state, "claim X", "reason", "scope", "notify")
    state["breaker_key"] = gb.breaker_key("S", "P")
    judge = RoutingJudge(grounded=1)
    disarmed, _, msg = gb.evaluate_post_tool_release(
        _pre("Read"), state, fresh_tool="[tool_result]\nok", judge=judge
    )
    assert disarmed is True
    assert state["breaker_provisional"] is False
    assert state["breaker_armed"] is False
    assert "breaker open" in msg.lower()


def test_loaded_skill_names_parses_skill_tool_use():
    seg = (
        '<record line="000001" type="assistant" role="assistant">\n'
        '[tool_use name=Skill]\n{"command": "release"}\n</record>\n'
        '<record line="000002" type="user" role="user">\n'
        "[tool_result]\nSuccessfully loaded skill\n</record>"
    )
    assert "release" in gb.loaded_skill_names(seg)
    assert gb.loaded_skill_names("no skill loaded here") == set()


def test_claim_describes_loaded_skill_requires_skill_context():
    seg = '[tool_use name=Skill]\n{"command": "release"}'
    assert gb.claim_describes_loaded_skill("the release skill handles X", seg) is True
    assert gb.claim_describes_loaded_skill("use skill: release to ship", seg) is True
    # bare skill-name word with no skill context is NOT suppressed (repo claim)
    assert gb.claim_describes_loaded_skill("the release workflow in ci.yml runs publish", seg) is False
    # no skill loaded -> never suppressed
    assert gb.claim_describes_loaded_skill("the release skill handles X", "no load") is False


def test_arm_judge_does_not_arm_on_just_loaded_skill_behavior():
    segment = (
        '<record line="000001" type="assistant" role="assistant">\n'
        '[tool_use name=Skill]\n{"command": "release"}\n</record>\n'
        '<record line="000002" type="user" role="user">\n'
        "[tool_result]\nSuccessfully loaded skill\n</record>"
    )

    def judge(system, user, schema):
        return {
            "verdict": 1,
            "steering": "ground the claim that the release skill handles the release tail",
            "claim": (
                "the release skill handles the full release tail end-to-end "
                "(commit, version bump, push, npm publish, verify)"
            ),
            "load_bearing": 1,
        }

    verdict, steering, claim = gb.arm_judge(segment, judge=judge)
    assert verdict == 0 and steering == "" and claim == ""


def test_arm_judge_still_arms_on_repo_claim_despite_loaded_skill():
    segment = '[tool_use name=Skill]\n{"command": "release"}\n[tool_result]\nok'

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
    segment = (
        '[tool_use name=Skill]\n{"command": "release"}\n'
        "[tool_result]\nSuccessfully loaded skill"
    )

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


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
