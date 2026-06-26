#!/usr/bin/env python3
"""T7: the groundedness breaker self-resolves find/read-checkable claims via the
explore search.sh (read-only) BEFORE arming, and de-escalates if the gathered
evidence grounds the claim -- instead of arming and forcing the agent to re-read.

De-escalate-only invariant: disabled flag, empty query, no search results, or any
error must leave the arm verdict intact (never add a block)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import breaker_judges as bj  # noqa: E402


def _arm_obj(**over):
    obj = {
        "load_bearing": 1,
        "verdict": 1,
        "steering": "ground it first",
        "claim": "no other live files reference the archived basenames",
        "verify": {"must_contain": [], "must_not_contain": []},
        "resolve_query": "live non-archive files referencing archived basenames",
        "directive": "",
        "tool_scope": {"allow": [], "deny": []},
    }
    obj.update(over)
    return obj


def _judge_returning(obj):
    def _fn(_system, _user, _schema):
        return obj

    return _fn


def test_self_resolve_de_escalates_when_grounded(monkeypatch):
    monkeypatch.setenv("UNIFABLE_BREAKER_SELF_RESOLVE", "1")
    monkeypatch.setattr(
        bj, "run_explore_search", lambda q, cwd, **k: '[{"path":"x.py","startLine":1,"endLine":2,"content":"only archive refs"}]'
    )
    monkeypatch.setattr(bj, "disarm_judge", lambda claim, seg, **k: bj.ReleaseVerdict(True, "", True, False, "", ""))
    out = bj.arm_judge("transcript with a confident enumeration claim", events=[], judge=_judge_returning(_arm_obj()), input_data={"cwd": "/tmp"})
    assert out == (0, "", "")


def test_self_resolve_keeps_arm_when_not_grounded(monkeypatch):
    monkeypatch.setenv("UNIFABLE_BREAKER_SELF_RESOLVE", "1")
    monkeypatch.setattr(bj, "run_explore_search", lambda q, cwd, **k: '[{"path":"y.py","content":"a live reference!"}]')
    monkeypatch.setattr(bj, "disarm_judge", lambda claim, seg, **k: bj.ReleaseVerdict(False, "read y.py", True, False, "", ""))
    verdict, _steering, claim = bj.arm_judge("transcript", events=[], judge=_judge_returning(_arm_obj()), input_data={"cwd": "/tmp"})
    assert verdict == 1 and claim


def test_self_resolve_failopen_when_search_empty(monkeypatch):
    called = {"judge": False}

    def _no_disarm(*_a, **_k):
        called["judge"] = True
        return bj.ReleaseVerdict(True, "", True, False, "", "")

    monkeypatch.setattr(bj, "run_explore_search", lambda q, cwd, **k: "")
    monkeypatch.setattr(bj, "disarm_judge", _no_disarm)
    verdict, _steering, _claim = bj.arm_judge("transcript", events=[], judge=_judge_returning(_arm_obj()), input_data={"cwd": "/tmp"})
    assert verdict == 1  # arm stands when the search returns nothing
    assert not called["judge"]  # release judge not consulted without snippets


def test_self_resolve_disabled_by_flag(monkeypatch):
    monkeypatch.setenv("UNIFABLE_BREAKER_SELF_RESOLVE", "0")
    called = {"search": False}

    def _no_search(*_a, **_k):
        called["search"] = True
        return "snippets"

    monkeypatch.setattr(bj, "run_explore_search", _no_search)
    verdict, _steering, _claim = bj.arm_judge("transcript", events=[], judge=_judge_returning(_arm_obj()), input_data={"cwd": "/tmp"})
    assert verdict == 1 and not called["search"]


def test_self_resolve_noop_without_resolve_query(monkeypatch):
    called = {"search": False}

    def _flag_search(*_a, **_k):
        called["search"] = True
        return "snip"

    monkeypatch.setattr(bj, "run_explore_search", _flag_search)
    verdict, _steering, _claim = bj.arm_judge(
        "transcript", events=[], judge=_judge_returning(_arm_obj(resolve_query="")), input_data={"cwd": "/tmp"}
    )
    assert verdict == 1 and not called["search"]


def test_run_explore_search_empty_when_script_missing(monkeypatch):
    import research_bash_guidance as rbg

    monkeypatch.setattr(rbg, "resolve_explore_search_sh", lambda: None)
    assert bj.run_explore_search("any query", "/tmp") == ""


def test_run_explore_search_empty_on_blank_query():
    assert bj.run_explore_search("   ", "/tmp") == ""


# --- gpt-realtime-2-authored command self-resolution (recon/exec lane) ---------


def _cmd_arm_obj(**over):
    base = {"resolve_query": "", "verify_cmd": "rg -q PATTERN scripts/x.py"}
    base.update(over)
    return _arm_obj(**base)


def test_command_self_resolve_de_escalates_on_exit0_and_grounded(monkeypatch):
    monkeypatch.setenv("UNIFABLE_BREAKER_SELF_RESOLVE", "1")
    import recon_lane

    monkeypatch.setattr(
        recon_lane,
        "run_validation_command",
        lambda cmd, cwd: {"ran": True, "allowed": True, "exit_code": 0, "output": "match", "reason": ""},
    )
    monkeypatch.setattr(bj, "disarm_judge", lambda claim, seg, **k: bj.ReleaseVerdict(True, "", True, False, "", ""))
    out = bj.arm_judge("transcript", events=[], judge=_judge_returning(_cmd_arm_obj()), input_data={"cwd": "/tmp"})
    assert out == (0, "", "")


def test_command_self_resolve_keeps_arm_on_nonzero_exit(monkeypatch):
    monkeypatch.setenv("UNIFABLE_BREAKER_SELF_RESOLVE", "1")
    import recon_lane

    called = {"judge": False}

    def _no_disarm(*_a, **_k):
        called["judge"] = True
        return bj.ReleaseVerdict(True, "", True, False, "", "")

    monkeypatch.setattr(
        recon_lane,
        "run_validation_command",
        lambda cmd, cwd: {"ran": True, "allowed": True, "exit_code": 1, "output": "", "reason": ""},
    )
    monkeypatch.setattr(bj, "disarm_judge", _no_disarm)
    verdict, _s, claim = bj.arm_judge("transcript", events=[], judge=_judge_returning(_cmd_arm_obj()), input_data={"cwd": "/tmp"})
    assert verdict == 1 and claim
    assert not called["judge"]  # release judge never consulted on non-zero exit


def test_command_self_resolve_keeps_arm_when_blocked(monkeypatch):
    monkeypatch.setenv("UNIFABLE_BREAKER_SELF_RESOLVE", "1")
    import recon_lane

    # A mutating command is gated by the host and never runs -> arm stands.
    monkeypatch.setattr(
        recon_lane,
        "run_validation_command",
        lambda cmd, cwd: {"ran": False, "allowed": False, "exit_code": None, "output": "", "reason": "not read-only"},
    )
    verdict, _s, _c = bj.arm_judge(
        "transcript", events=[], judge=_judge_returning(_cmd_arm_obj(verify_cmd="rm -rf x")), input_data={"cwd": "/tmp"}
    )
    assert verdict == 1


def test_command_self_resolve_noop_without_verify_cmd(monkeypatch):
    monkeypatch.setenv("UNIFABLE_BREAKER_SELF_RESOLVE", "1")
    import recon_lane

    called = {"run": False}

    def _flag_run(*_a, **_k):
        called["run"] = True
        return {"ran": True, "allowed": True, "exit_code": 0, "output": "", "reason": ""}

    monkeypatch.setattr(recon_lane, "run_validation_command", _flag_run)
    verdict, _s, _c = bj.arm_judge(
        "transcript", events=[], judge=_judge_returning(_cmd_arm_obj(verify_cmd="")), input_data={"cwd": "/tmp"}
    )
    assert verdict == 1 and not called["run"]

