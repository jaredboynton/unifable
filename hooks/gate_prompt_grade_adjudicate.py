#!/usr/bin/env python3
"""UserPromptSubmit — proactive judge-backed HEAVY grade adjudication.

Runs after gate_prompt when effective grade is HEAVY. Fails open on any error.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

from grade_override import try_adjudicate_grade
from ledger import emit_json, read_stdin_json


def main() -> int:
    input_data = read_stdin_json()
    prompt = str(input_data.get("prompt") or input_data.get("user_prompt") or "")
    context = try_adjudicate_grade(input_data, prompt)
    if context:
        emit_json(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )
    else:
        emit_json({})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        emit_json({"systemMessage": f"unifable grade adjudicate hook failed open: {exc}"})
        raise SystemExit(0)
