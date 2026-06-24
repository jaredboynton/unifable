#!/usr/bin/env python3
"""PostToolUse additionalContext dedup and cite-only vs action digest split."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(REPO / "scripts" / "gate"))

import model_notify as mn  # noqa: E402
from ledger import load_ledger, save_ledger  # noqa: E402
from posttool_notify import emit_posttool_context  # noqa: E402
from spec import save_spec, spec_template  # noqa: E402

JUDGE_REASON = "The current check is uncheckable without a repo-backed plan artifact."


def _sample_spec() -> dict:
    spec = spec_template()
    spec["requires_tasks"] = True
    spec["restated_goal"] = "Ship PostToolUse dedup"
    spec["tasks"] = [
        {
            "id": "T1",
            "title": "Plan artifact exists",
            "check": "test -f plan.md",
            "status": "failed",
            "judge_reason": JUDGE_REASON,
        },
    ]
    return spec


def _run_post_tool(payload: dict) -> dict:
    import gate_post_tool

    with patch.object(gate_post_tool, "read_stdin_json", lambda: payload):
        with patch("posttool_notify.emit_json") as emit:
            gate_post_tool.main()
            if emit.call_count:
                return emit.call_args[0][0]
            return {}


def _read_payload(
    tmp: str,
    session_id: str,
    file_path: Path,
    *,
    turn_id: str = "turn-read-1",
) -> dict:
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": tmp,
        "tool_name": "Read",
        "tool_input": {"file_path": str(file_path)},
        "tool_response": {"content": file_path.read_text(encoding="utf-8")},
    }


def test_citation_sync_emits_headline_only():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        f = Path(tmp) / "hooks.json"
        f.write_text("{}\n", encoding="utf-8")
        spec = _sample_spec()
        save_spec(tmp, "cite-only-test", spec)
        out = _run_post_tool(_read_payload(tmp, "cite-only-test", f))
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        assert ctx.startswith("synced 1 cite(s):")
        assert "repo_context<-read" in ctx
        assert "T1:" not in ctx
        assert JUDGE_REASON not in ctx


def test_citation_sync_no_repeat_same_guidance():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        f1 = Path(tmp) / "a.py"
        f1.write_text("# a\n", encoding="utf-8")
        f2 = Path(tmp) / "b.py"
        f2.write_text("# b\n", encoding="utf-8")
        spec = _sample_spec()
        save_spec(tmp, "cite-repeat-test", spec)

        payload = {
            "session_id": "cite-repeat-test",
            "turn_id": "turn-repeat",
            "cwd": tmp,
        }
        ledger = load_ledger(payload)
        ledger["posttool_task_guidance"] = {
            "T1": mn.task_guidance_fingerprint(spec, "T1"),
        }
        save_ledger(payload, ledger)

        out1 = _run_post_tool(_read_payload(tmp, "cite-repeat-test", f1))
        ctx1 = (out1.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        assert "synced 1 cite(s):" in ctx1
        assert "T1:" not in ctx1

        out2 = _run_post_tool(_read_payload(tmp, "cite-repeat-test", f2))
        ctx2 = (out2.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        assert "T1:" not in ctx2
        assert JUDGE_REASON not in ctx2


def test_spec_cli_still_emits_action_digest():
    spec = _sample_spec()
    buf = io.StringIO()
    with redirect_stderr(buf):
        mn.notify_spec_update(
            spec,
            "Requirement T9 added: follow-up.",
            highlight_task="T1",
        )
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        payload = {
            "session_id": "spec-cli-digest",
            "turn_id": "turn-cli",
            "cwd": tmp,
            "tool_name": "Bash",
            "tool_input": {
                "command": "unifable add-task --title follow --check true",
            },
            "tool_response": {
                "exit_code": 0,
                "stdout": "Added T9",
                "stderr": buf.getvalue(),
            },
        }
        out = _run_post_tool(payload)
        ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
        assert "Requirement T9 added" in ctx
        assert "T1:" in ctx
        assert JUDGE_REASON in ctx


def test_parallel_identical_body_deduped(capsys):
    input_data = {
        "session_id": "posttool-dedup",
        "turn_id": "turn-par",
        "cwd": os.getcwd(),
    }
    body = "synced 1 cite(s): repo_context<-read [a.py:1]"

    def one_emit() -> str:
        with patch("posttool_notify.emit_json") as emit:
            emit_posttool_context(input_data, body)
            if emit.call_count:
                payload = emit.call_args[0][0]
                return (payload.get("hookSpecificOutput") or {}).get("additionalContext") or ""
            return ""

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["UNIFABLE_DATA"] = tmp
        input_data = {**input_data, "cwd": tmp}
        results: list[str] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(one_emit) for _ in range(6)]
            for fut in as_completed(futures):
                results.append(fut.result())
        non_empty = [r for r in results if r.strip()]
        assert len(non_empty) == 1
        assert non_empty[0] == body


def test_action_digest_emits_on_judge_reason_change():
    spec = _sample_spec()
    ledger: dict = {"posttool_task_guidance": {}}
    first, cache = mn.format_spec_action_digest_delta(spec, ledger)
    assert "T1:" in first
    assert JUDGE_REASON in first
    ledger["posttool_task_guidance"] = cache

    second, _ = mn.format_spec_action_digest_delta(spec, ledger)
    assert second == ""

    spec["tasks"][0]["judge_reason"] = "Revise the check to target the plan deliverable."
    third, updated = mn.format_spec_action_digest_delta(spec, ledger)
    assert "Revise the check" in third
    assert updated["T1"]["reason"] != cache["T1"]["reason"]


def test_build_spec_context_from_spec_can_omit_action():
    spec = _sample_spec()
    ctx = mn.build_spec_context_from_spec(
        spec,
        headlines=["synced 2 cite(s): repo_context<-read [x.py:1]"],
        include_action=False,
    )
    assert ctx.startswith("synced 2 cite(s):")
    assert "T1:" not in ctx


def test_judge_heal_includes_plan_mode_context(monkeypatch, tmp_path):
    from spec import judge_heal_own_requirements

    tx = tmp_path / "session.jsonl"
    tx.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_started",
                    "collaboration_mode_kind": "plan",
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    spec = {
        "requires_tasks": True,
        "restated_goal": "g",
        "tasks": [
            {
                "id": "T9",
                "title": "needs repo file",
                "check": "test -f plan.md",
                "status": "failed",
                "added_by": "judge",
            }
        ],
    }
    captured: dict = {}

    def fake_ask(system, _user, _schema, schema_name=""):
        captured["system"] = system
        assert schema_name == "judge_heal"
        return {"adjust_requirements": []}

    import codex_judge

    monkeypatch.setattr(codex_judge, "ask_structured", fake_ask)
    judge_heal_own_requirements(spec, transcript_path=str(tx))
    assert "PLAN MODE" in captured.get("system", "")
    assert "plan_mode_enabled" in captured.get("system", "")
