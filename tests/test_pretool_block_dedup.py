#!/usr/bin/env python3
"""Tests for PreToolUse block compression and parallel deduplication."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GATE = REPO / "scripts" / "gate"
HOOKS = REPO / "hooks"
PY = sys.executable

sys.path.insert(0, str(GATE))

import pretool_block as pb  # noqa: E402
from pretool_block import (  # noqa: E402
    compact_pretool_output,
    emit_pretool_block,
    format_bash_research_block,
)


def _bash_payload(
    *,
    command: str = "nl",
    session_id: str = "pretool-dedup-test",
    turn_id: str = "turn-parallel-1",
    cwd: str | None = None,
) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": session_id,
        "turn_id": turn_id,
        "cwd": cwd or os.getcwd(),
        "permission_mode": "bypassPermissions",
    }


def _run_pre_tool(payload: dict, *, data_root: str) -> tuple[int, str]:
    env = dict(os.environ)
    env["UNIFABLE_GRADE"] = "STANDARD"
    env["UNIFABLE_VERIFY_CITATIONS"] = "0"
    env["UNIFABLE_DATA"] = data_root
    proc = subprocess.run(
        [PY, str(HOOKS / "pre_tool_use.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stderr


def test_bash_block_message_size_under_budget():
    msg = format_bash_research_block("nl is not in the Bash research whitelist", "sess-1")
    assert len(msg) < 650
    assert "Bash blocked" not in msg
    assert "unifable restate" in msg
    assert "Allowed now:" in msg


def test_sequential_same_signature_second_and_third_are_silent():
    with tempfile.TemporaryDirectory() as tmp:
        payload = _bash_payload(cwd=tmp)
        rc1, err1 = _run_pre_tool(payload, data_root=tmp)
        rc2, err2 = _run_pre_tool(payload, data_root=tmp)
        rc3, err3 = _run_pre_tool(payload, data_root=tmp)
        assert rc1 == 2 and rc2 == 2 and rc3 == 2
        assert "nl is not in the Bash research whitelist" in err1 or "Unlock:" in err1
        assert err2.strip() == ""
        assert err3.strip() == ""


def test_parallel_blocks_emit_one_stderr():
    with tempfile.TemporaryDirectory() as tmp:
        payload = _bash_payload(cwd=tmp)

        def one_run() -> tuple[int, str]:
            return _run_pre_tool(payload, data_root=tmp)

        results: list[tuple[int, str]] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(one_run) for _ in range(6)]
            for fut in as_completed(futures):
                results.append(fut.result())

        assert len(results) == 6
        assert all(rc == 2 for rc, _ in results)
        non_empty = [err for _, err in results if err.strip()]
        assert len(non_empty) == 1
        assert "nl is not in the Bash research whitelist" in non_empty[0] or "Unlock:" in non_empty[0]


def test_epoch_reset_allows_full_message_again():
    with tempfile.TemporaryDirectory() as tmp:
        payload = _bash_payload(cwd=tmp)
        rc1, err1 = _run_pre_tool(payload, data_root=tmp)
        assert rc1 == 2
        assert "nl is not in the Bash research whitelist" in err1 or "Unlock:" in err1

        from ledger import load_ledger, save_ledger  # noqa: E402

        os.environ["UNIFABLE_DATA"] = tmp
        # Storage moved to the consolidated DB; reset the dedup epoch via the
        # ledger accessors rather than editing a JSON file.
        ledger = load_ledger(payload)
        ledger["pretool_block_epoch"] = ""
        ledger["pretool_block_counts"] = {}
        ledger["pretool_unlock_footer_epoch"] = ""
        save_ledger(payload, ledger)

        rc2, err2 = _run_pre_tool(payload, data_root=tmp)
        assert rc2 == 2
        assert "nl is not in the Bash research whitelist" in err2 or "Unlock:" in err2


def test_emit_fail_open_still_blocks_with_message(monkeypatch):
    input_data = {"session_id": "fail-open", "cwd": "/tmp", "turn_id": "t1"}

    @contextlib.contextmanager
    def boom(_input_data):
        raise RuntimeError("lock failed")
        yield

    monkeypatch.setattr("pretool_block._pretool_lock", boom)
    rc = emit_pretool_block(
        input_data,
        kind="bash",
        detail="nl",
        full_message="npm is not in the Bash research whitelist.",
    )
    assert rc == 2


def test_emit_pretool_block_has_no_channel_prefix(capsys):
    input_data = {"session_id": "no-prefix", "cwd": "/tmp", "turn_id": "t1"}
    rc = emit_pretool_block(
        input_data,
        kind="bash",
        detail="nl",
        full_message="npm is not in the Bash research whitelist.",
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert not err.startswith("unifable pre-edit gate:")
    assert "npm is not in the Bash research whitelist." in err or not err


def test_mixed_block_kinds_second_is_compact_not_full_footer(tmp_path, capsys):

    os.environ["UNIFABLE_DATA"] = str(tmp_path)
    payload = {"session_id": "mix-kind", "cwd": str(tmp_path), "turn_id": "turn-1"}
    rc1 = pb.emit_pretool_block(
        payload,
        kind="bash",
        detail="nl",
        full_message=pb.format_bash_research_block("nl blocked", "mix-kind"),
    )
    assert rc1 == 2
    err1 = capsys.readouterr().err
    assert pb._RESTATE_LINE in err1
    assert pb._ADD_TASK_LINE in err1
    rc2 = pb.emit_pretool_block(
        payload,
        kind="delegate",
        detail="Task",
        full_message=pb.format_delegation_block("Task", "mix-kind", ctx=pb.block_context(payload)),
    )
    assert rc2 == 2
    err2 = capsys.readouterr().err
    assert "Unlock:" not in err2
    assert err2.strip() == ""

    with tempfile.TemporaryDirectory() as tmp:
        base = _bash_payload(cwd=tmp)
        payloads = [
            {**base, "tool_input": {"command": "nl"}},
            {**base, "tool_input": {"command": "curl example.com"}},
            {**base, "tool_input": {"command": "nl"}},
            {**base, "tool_input": {"command": "curl example.com"}},
        ]

        def one_run(payload: dict) -> tuple[int, str]:
            return _run_pre_tool(payload, data_root=tmp)

        results: list[tuple[int, str]] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(one_run, p) for p in payloads]
            for fut in as_completed(futures):
                results.append(fut.result())

        assert all(rc == 2 for rc, _ in results)
        non_empty = [err for _, err in results if err.strip()]
        assert len(non_empty) == 2


def test_compact_citation_keeps_cite_lines_when_footer_sent():
    msg = (
        "spec citations are not backed by real activity this session:\n"
        "  repo_context[0]: 'hooks/pre_tool_use.py:1' (never read this session)\n"
        "  prior_art[0]: 'https://example.com' (never fetched this session)\n"
        "\n"
        "Read each cited file (Read/grep) before citing it."
    )
    out = compact_pretool_output(msg, footer_sent=True)
    assert "repo_context[0]" in out
    assert "prior_art[0]" in out
    assert "Read each cited file" not in out
