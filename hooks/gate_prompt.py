#!/usr/bin/env python3
"""unifable observation gate — UserPromptSubmit.

Classifies the new prompt's task mode and resets the per-prompt ledger so the
Stop gate judges only this turn's evidence. Fails open (emits {} on any error).
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

from ledger import add_unique, emit_json, read_stdin_json, update_ledger
from classify_task import classify_prompt, context_for_mode, grade_of
from spec import all_tasks_validated, load_spec


def _prompt_key(prompt: str) -> str:
    """Stable per-task key = sha256(prompt) prefix. Specs are keyed by this, so a
    distinct prompt seeds a distinct spec (multiple specs per session)."""
    return hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()[:16]


def main() -> int:
    input_data = read_stdin_json()
    prompt = str(input_data.get("prompt") or input_data.get("user_prompt") or "")
    mode, risks = classify_prompt(prompt)
    cwd = input_data.get("cwd") or os.getcwd()
    new_key = _prompt_key(prompt)

    def apply(ledger):
        # Active spec key (locked-until-complete): keep the current active spec
        # while it still has unvalidated tasks; otherwise this prompt seeds a new
        # one. A missing/complete/no-task active spec is not a lock.
        active = ledger.get("active_task")
        locked = False
        if active:
            try:
                spec = load_spec(cwd, active)
                if spec is not None and not all_tasks_validated(spec)[0]:
                    locked = True
            except Exception:
                locked = False
        if not locked:
            ledger["active_task"] = new_key
        ledger["task_mode"] = mode
        ledger["grade"] = grade_of(mode)
        ledger["warning_count"] = 0
        ledger["warnings"] = []
        ledger["changed_files_seen"] = False
        ledger["change_kinds"] = []
        ledger["risk_flags"] = []
        ledger["verification_commands"] = []
        ledger["verification_results"] = []
        ledger["failures"] = []
        ledger["stop_blocks"] = 0
        add_unique(ledger, "risk_flags", risks)

    update_ledger(input_data, apply)

    emit_json(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context_for_mode(mode, risks),
            }
        }
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never block on our own bug
        emit_json({"systemMessage": f"unifable gate prompt hook failed open: {exc}"})
        raise SystemExit(0)
