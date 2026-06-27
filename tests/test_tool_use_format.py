#!/usr/bin/env python3
"""Golden cases ported from patchpress scripts/test-tool-use-format.mjs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "gate"
sys.path.insert(0, str(GATE))

from tool_use_format import (  # noqa: E402
    format_diff_lines_result,
    format_edit_tool,
    format_tool_result,
    format_tool_result_content,
    format_tool_use,
    is_formatted_edit_text,
    line_diff,
)


def test_edit_tool_formats_as_diff_block():
    cwd = "/Users/jaredboynton/__devlocal/llm/"
    meta = {"lineNumber": 501, "recordHash": "abc123", "cwdPrefix": cwd}
    edit_input = {
        "file_path": cwd + "src/llm/providers/codex_rs_wire.py",
        "old_str": (
            "from __future__ import annotations\n\n"
            "from typing import Any, Literal\n\n"
            'CODEX_ORIGINATOR = "codex_cli_rs"\n'
        ),
        "new_str": (
            "from __future__ import annotations\n\n"
            "import json\n"
            "from typing import Any, Literal\n\n"
            'CODEX_ORIGINATOR = "codex_cli_rs"\n'
        ),
    }
    edit_formatted = format_tool_use({"type": "tool_use", "name": "Edit", "input": edit_input}, meta)
    assert is_formatted_edit_text(edit_formatted)
    assert "\\n" not in edit_formatted
    assert "@@file src/llm/providers/codex_rs_wire.py" in edit_formatted
    assert "+import json" in edit_formatted
    assert "stats: +" in edit_formatted


def test_str_replace_diff():
    out = format_edit_tool(
        "StrReplace",
        {"file_path": "/tmp/example.py", "old_str": "before", "new_str": "after"},
        {"lineNumber": 12},
    )
    assert "-before" in out
    assert "+after" in out


def test_diff_lines_result_from_json():
    cwd = "/Users/jaredboynton/__devlocal/llm/"
    meta = {"lineNumber": 501, "recordHash": "abc123", "cwdPrefix": cwd}
    diff_lines_obj = {
        "success": True,
        "file_path": cwd + "src/llm/providers/codex_responses_client.py",
        "diffLines": [
            {"type": "unchanged", "content": "CODEX_ORIGINATOR = os.environ.get("},
            {"type": "unchanged", "content": '    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "codex_cli_rs"'},
            {"type": "unchanged", "content": ")"},
            {"type": "added", "content": 'CODEX_TUI_ORIGINATOR = "codex-tui"'},
            {"type": "unchanged", "content": 'CODEX_RESPONSES_WS_BETA = "responses_websockets=2026-02-06"'},
        ],
    }
    formatted = format_diff_lines_result(diff_lines_obj, {**meta, "toolName": "EditResult"})
    assert "@@tool EditResult" in formatted
    assert "+CODEX_TUI_ORIGINATOR" in formatted
    assert '"diffLines"' not in formatted


def test_tool_result_content_parses_diff_lines_json():
    cwd = "/Users/jaredboynton/__devlocal/llm/"
    meta = {"lineNumber": 501, "recordHash": "abc123", "cwdPrefix": cwd}
    diff_lines_obj = {
        "file_path": cwd + "src/llm/providers/codex_responses_client.py",
        "diffLines": [{"type": "added", "content": "+CODEX_TUI_ORIGINATOR = \"codex-tui\""}],
    }
    out = format_tool_result_content(json.dumps(diff_lines_obj), meta)
    assert "+CODEX_TUI_ORIGINATOR" in out


def test_tool_result_part():
    cwd = "/Users/jaredboynton/__devlocal/llm/"
    meta = {"lineNumber": 501, "recordHash": "abc123", "cwdPrefix": cwd}
    diff_lines_obj = {
        "file_path": cwd + "src/llm/providers/codex_responses_client.py",
        "diffLines": [{"type": "added", "content": 'CODEX_TUI_ORIGINATOR = "codex-tui"'}],
    }
    out = format_tool_result(
        {"type": "tool_result", "tool_use_id": "tooluse_test", "content": json.dumps(diff_lines_obj)},
        meta,
    )
    assert "EditResult" in out
    assert '"diffLines"' not in out


def test_line_diff_marks_add_and_remove():
    pairs = line_diff("alpha\nbeta\ngamma", "alpha\nBETA\ngamma")
    assert any(p["type"] == "remove" for p in pairs)
    assert any(p["type"] == "add" for p in pairs)


def test_generic_tool_use_header():
    out = format_tool_use(
        {"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}},
        {"lineNumber": 3},
    )
    assert out.startswith("@@tool Bash")
    assert "echo hi" in out
