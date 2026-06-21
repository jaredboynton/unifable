#!/usr/bin/env python3
"""Logic tests for the overconfidence / groundedness breaker (groundedness.py).

Each test maps to a requirement of the breaker:
  R1  block ONLY Write/Edit/Bash; never WebSearch/Read/WebFetch/Grep/Glob
  R2  arm (strict judge, verdict 1) and disarm (separate claim-bound release judge)
  R5  judge question = "did the model say something confidently w/o backing it up"
  R6  verdict 1 -> steering prompt returned + mutation blocked until evidence read
  R7  verdict 0 -> no steering, model sees nothing (no block)
  R8  release gated on NEW grounding activity; release judge bound to the claim
  R9  arm judge debounced: <=1 call / 15s per session+user-prompt key
  R10 safety cap: after max_blocks consecutive blocks on one arm, fail open
Run: python3 -m pytest tests/test_groundedness_breaker.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import groundedness as gb  # noqa: E402


# --- a recording judge: scripted verdicts + a call counter for debounce tests ---
class FakeJudge:
    def __init__(self, script):
        self.script = list(script)  # list of (verdict, steering)
        self.calls = 0
        self.systems = []

    def __call__(self, system, user, schema):
        self.systems.append(system)
        self.calls += 1
        verdict, steering = self.script[min(self.calls - 1, len(self.script) - 1)]
        return {"verdict": verdict, "steering": steering, "claim": "claim" if verdict == 1 else ""}


# --- a routing judge: ARM system -> {verdict,steering,claim}; DISARM system ->
#     {grounded}. Counts arm vs disarm calls separately for the release tests. ---
class RoutingJudge:
    def __init__(self, arm=(1, "blocked", "the cause is Y"), grounded=1, needed="read foo.py:10 and cite it"):
        self.arm_ret = arm
        self.grounded = grounded
        self.needed = needed
        self.arm_calls = 0
        self.disarm_calls = 0

    def __call__(self, system, user, schema):
        if "release monitor" in system.lower():  # _DISARM_SYSTEM
            self.disarm_calls += 1
            if self.grounded:
                return {"grounded": 1, "needed": ""}
            return {"grounded": 0, "needed": self.needed}
        self.arm_calls += 1
        v, s, c = self.arm_ret
        return {"verdict": v, "steering": s, "claim": c}


def _pre(tool, session="S", transcript="model: clearly the fix is X. (no evidence)"):
    # input_data shaped like a PreToolUse hook payload; transcript injected directly
    # by monkeypatching transcript_segment in tests that need a non-empty segment.
    return {"tool_name": tool, "session_id": session, "cwd": "/repo"}


# --------------------------------------------------------------------------- R1
def test_mutation_set_is_exactly_writes_edits_bash():
    for t in ("Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "Bash"):
        assert gb.is_mutation_tool(t), t
    for t in ("WebSearch", "Read", "WebFetch", "Grep", "Glob", "Task", "TodoWrite"):
        assert not gb.is_mutation_tool(t), t


def test_read_and_websearch_never_blocked_even_when_armed(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "model: definitely the cause is Y")
    judge = FakeJudge([(1, "you claimed Y with no proof; mutation blocked")])
    state = {}
    # arm via a mutation attempt first
    blocked, _ = gb.evaluate(_pre("Bash"), state, now=100.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    # now a Read within the debounce window: armed, but reads are never blocked
    blocked, steering = gb.evaluate(_pre("Read"), state, now=101.0, active_task="P", judge=judge)
    assert blocked is False and steering == ""
    # WebSearch likewise
    blocked, _ = gb.evaluate(_pre("WebSearch"), state, now=102.0, active_task="P", judge=judge)
    assert blocked is False


# --------------------------------------------------------------------------- R6
def test_verdict1_blocks_mutation_and_returns_steering(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "model: the fix is obviously Z")
    judge = FakeJudge([(1, "You asserted Z without reading the source. Write/Edit/Bash blocked until you cite evidence.")])
    state = {}
    blocked, steering = gb.evaluate(_pre("Edit"), state, now=10.0, active_task="P", judge=judge)
    assert blocked is True
    assert "blocked" in steering.lower() and steering != ""


# --------------------------------------------------------------------------- R7
def test_verdict0_no_block_no_steering(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "model: I read foo.py:10, it does X; here is the edit")
    judge = FakeJudge([(0, "")])
    state = {}
    blocked, steering = gb.evaluate(_pre("Write"), state, now=10.0, active_task="P", judge=judge)
    assert blocked is False and steering == ""
    assert state["breaker_armed"] is False


# --------------------------------------------------------------------------- R2/R8
def test_arms_then_disarms_via_release_judge(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "unproven; blocked", "the cause is Y"), grounded=1)
    state = {}
    blocked, _ = gb.evaluate(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    assert judge.arm_calls == 1 and judge.disarm_calls == 0
    # model actually reads evidence -> ledger grounding activity grows
    state["read_paths"] = ["/repo/foo.py"]
    blocked, steering = gb.evaluate(_pre("Bash"), state, now=20.0, active_task="P", judge=judge)
    assert blocked is False and steering == "" and state["breaker_armed"] is False
    assert judge.disarm_calls == 1  # release decided by the dedicated disarm judge


def test_armed_with_no_new_activity_stays_blocked_without_judging(monkeypatch):
    # R8: no new grounding activity -> the disarm judge is NEVER consulted
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "blocked", "claim X"), grounded=1)
    state = {}
    gb.evaluate(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    assert state["breaker_armed"] is True
    # retry with NO activity growth: still blocked, disarm judge not called
    blocked, _ = gb.evaluate(_pre("Edit"), state, now=99.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    assert judge.disarm_calls == 0


def test_armed_new_activity_but_not_grounded_stays_blocked(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(
        arm=(1, "blocked", "claim X"), grounded=0, needed="still missing: read codex_judge.py:54 and cite MODEL"
    )
    state = {}
    gb.evaluate(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    state["read_paths"] = ["/repo/unrelated.py"]  # read something, but not grounding the claim
    blocked, steering = gb.evaluate(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    assert judge.disarm_calls == 1
    assert state["breaker_block_count"] >= 1
    # the block message refreshes with exactly what is still missing to disarm
    assert steering == "still missing: read codex_judge.py:54 and cite MODEL"


# --------------------------------------------------------------------------- R9
def test_arm_judge_debounced_at_most_once_per_15s_per_key(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")
    judge = RoutingJudge(arm=(0, "", ""))  # verdict 0 -> never arms, stays on ARM path
    state = {}
    gb.evaluate(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    gb.evaluate(_pre("Bash"), state, now=5.0, active_task="P", judge=judge)   # within window
    gb.evaluate(_pre("Bash"), state, now=14.9, active_task="P", judge=judge)  # within window
    assert judge.arm_calls == 1
    gb.evaluate(_pre("Bash"), state, now=15.0, active_task="P", judge=judge)  # window elapsed
    assert judge.arm_calls == 2


# --------------------------------------------------------------------------- R10
def test_safety_cap_fails_open_after_max_blocks(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    monkeypatch.setenv("UNIFABLE_BREAKER_MAX_BLOCKS", "3")
    judge = RoutingJudge(arm=(1, "blocked", "claim X"), grounded=0)  # never grounds
    state = {}
    b1, _ = gb.evaluate(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)  # arm, block count 1
    b2, _ = gb.evaluate(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)  # block count 2
    b3, _ = gb.evaluate(_pre("Edit"), state, now=2.0, active_task="P", judge=judge)  # count 3 -> release
    assert b1 is True and b2 is True
    assert b3 is False and state["breaker_armed"] is False  # failed open


def test_new_user_prompt_drops_stale_arm(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(0, "", ""))  # under the NEW prompt, no violation
    state = {}
    # arm under prompt P1
    gb.arm(state, gb.breaker_key("S", "P1"), 0.0, "blocked", "claim X")
    assert state["breaker_armed"] is True
    # a mutation under a NEW prompt P2 -> stale arm dropped, re-judged clean -> allowed
    blocked, _ = gb.evaluate(_pre("Edit", session="S"), state, now=1.0, active_task="P2", judge=judge)
    assert blocked is False and state["breaker_armed"] is False
    assert judge.arm_calls == 1


def test_debounce_key_is_session_plus_user_prompt(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")
    judge = FakeJudge([(1, "blocked")])
    state = {}
    gb.evaluate(_pre("Bash", session="S"), state, now=0.0, active_task="P1", judge=judge)
    # same session, NEW user prompt -> different key -> re-judge even within 15s
    gb.evaluate(_pre("Bash", session="S"), state, now=1.0, active_task="P2", judge=judge)
    assert judge.calls == 2
    # new session, same prompt -> different key -> re-judge
    gb.evaluate(_pre("Bash", session="S2"), state, now=1.5, active_task="P2", judge=judge)
    assert judge.calls == 3


# --------------------------------------------------------------------------- fail-open
def test_judge_exception_fails_open(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "t")

    def boom(system, user, schema):
        raise RuntimeError("realtime down")

    state = {}
    blocked, steering = gb.evaluate(_pre("Bash"), state, now=0.0, active_task="P", judge=boom)
    assert blocked is False and steering == ""


def test_disabled_env_fails_open(monkeypatch):
    monkeypatch.setenv("UNIFABLE_BREAKER", "0")
    judge = FakeJudge([(1, "blocked")])
    state = {}
    blocked, _ = gb.evaluate(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    assert blocked is False and judge.calls == 0


# --------------------------------------------------------------------------- R5 (prompt) / judge plumbing
def test_judge_system_prompt_asks_the_confidence_question():
    seg = "model said something"
    judge = FakeJudge([(1, "x")])
    gb.judge_segment(seg, judge=judge)
    sysp = judge.systems[0].lower()
    assert "confidently without backing it up" in sysp
    # steering names the allowed read-only set, not the blocked mutation tools
    assert "restricted to read-only" in sysp
    assert "read, websearch, webfetch, grep, glob" in sysp


def test_arm_prompt_does_not_arm_on_retraction_or_aside():
    # The arm prompt must instruct NOT to fire on a retracted/corrected claim or a
    # non-load-bearing aside (the BENCHMARKS.md failure mode).
    sysp = gb._JUDGE_SYSTEM.lower()
    assert "retract" in sysp
    assert "load-bearing" in sysp


def test_disarm_prompt_releases_on_retraction_and_bounded_negative():
    # The release prompt must accept retraction and a bounded search of a negative
    # claim, and must NOT demand proof of a universal negative.
    sysp = gb._DISARM_SYSTEM.lower()
    assert "retract" in sysp
    assert "negative" in sysp and "bounded search" in sysp
    assert "universal negative" in sysp


def test_arm_prompt_steers_external_claims_to_documentation():
    # A claim about host/platform/API behavior is grounded by authoritative external
    # docs (web search / WebFetch), NOT by a repo file like AGENTS.md. The arm prompt
    # must tell the judge to match the source to the claim and steer accordingly.
    sysp = gb._JUDGE_SYSTEM.lower()
    assert "external" in sysp and "documentation" in sysp
    assert "web search" in sysp or "webfetch" in sysp
    # the schema's steering field must offer the external-doc source class too
    desc = gb._JUDGE_SCHEMA["properties"]["steering"]["description"].lower()
    assert "documentation" in desc


def test_disarm_prompt_releases_on_fetched_external_documentation():
    # The release prompt must accept fetched + quoted authoritative external docs as
    # grounding for an external/platform claim, and must not demand a repo file:line
    # for a claim whose truth lives in external documentation.
    sysp = gb._DISARM_SYSTEM.lower()
    assert "external" in sysp and "documentation" in sysp
    assert "fetch" in sysp


def test_empty_segment_is_not_a_violation():
    judge = FakeJudge([(1, "should not be used")])
    verdict, steering = gb.judge_segment("", judge=judge)
    assert verdict == 0 and steering == "" and judge.calls == 0


def test_verdict0_forces_empty_steering_even_if_model_returns_text():
    # defense-in-depth: a verdict-0 with stray steering text must not leak a prompt
    judge = FakeJudge([(0, "stray text that should be dropped")])
    verdict, steering = gb.judge_segment("seg", judge=judge)
    assert verdict == 0 and steering == ""


# --------------------------------------------------------------------------- transcript
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
        }) + "\n" +
        json.dumps({
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


# --------------------------------------------------------------------------- Adjudicated Claims memory
def test_disarm_adds_to_adjudicated_claims(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    judge = RoutingJudge(arm=(1, "blocked", "claim to disarm"), grounded=1)
    state = {}
    
    # Arm it
    blocked, _ = gb.evaluate(_pre("Bash"), state, now=0.0, active_task="P", judge=judge)
    assert blocked is True and state["breaker_armed"] is True
    
    # Sim activity growth -> disarm
    state["read_paths"] = ["/repo/foo.py"]
    blocked, _ = gb.evaluate(_pre("Bash"), state, now=20.0, active_task="P", judge=judge)
    assert blocked is False and state["breaker_armed"] is False
    
    # Verify it is in adjudicated list
    assert "claim to disarm" in state.get("breaker_adjudicated_claims", [])


def test_safety_cap_adds_to_adjudicated_claims(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    monkeypatch.setenv("UNIFABLE_BREAKER_MAX_BLOCKS", "3")
    judge = RoutingJudge(arm=(1, "blocked", "uncapped claim"), grounded=0)
    state = {}
    
    b1, _ = gb.evaluate(_pre("Edit"), state, now=0.0, active_task="P", judge=judge)
    b2, _ = gb.evaluate(_pre("Edit"), state, now=1.0, active_task="P", judge=judge)
    b3, _ = gb.evaluate(_pre("Edit"), state, now=2.0, active_task="P", judge=judge)
    assert b1 is True and b2 is True and b3 is False
    
    # Verify failed-open claim is also in adjudicated list
    assert "uncapped claim" in state.get("breaker_adjudicated_claims", [])


def test_adjudicated_claims_prevents_re_arm(monkeypatch):
    monkeypatch.setattr(gb, "transcript_segment", lambda d, **k: "transcript")
    
    # If the claim is already adjudicated, it should not re-arm (verdict should be overridden/ignored)
    state = {
        "breaker_adjudicated_claims": ["my claim"]
    }
    
    # Let the judge return verdict=1 for the same claim
    called_system_prompt = []
    def recording_judge(system, user, schema):
        called_system_prompt.append(system)
        return {"verdict": 1, "steering": "blocked", "claim": "my claim"}
        
    blocked, _ = gb.evaluate(_pre("Edit"), state, now=100.0, active_task="P", judge=recording_judge)
    
    # Should not be blocked because the claim was already adjudicated
    assert blocked is False
    assert state.get("breaker_armed") is False
    
    # Check that system prompt included the adjudicated claims
    assert len(called_system_prompt) == 1
    assert "Do NOT flag any of the following claims" in called_system_prompt[0]
    assert "- my claim" in called_system_prompt[0]


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-q"]))
