#!/usr/bin/env python3
"""unifable observation gate — PostToolUse.

Records observed evidence after each Bash/Edit/Write tool call: whether files
changed (and their kind), and whether a verification command ran and observably
succeeded or failed. Fails open.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

from ledger import add_unique, emit_json, read_stdin_json, update_ledger
from parse_tool_result import (
    changed_kinds,
    command_from_input,
    detect_failure,
    fetched_url_targets,
    ran_command,
    read_targets,
    repeated_failure,
    verification_record,
)


def _abs(path: str, cwd: str) -> str:
    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path(cwd) / p
        return str(p.resolve())
    except (OSError, ValueError):
        return path


def main() -> int:
    input_data = read_stdin_json()
    cwd = str(input_data.get("cwd") or os.getcwd())
    kinds = changed_kinds(input_data)
    failure = detect_failure(input_data)
    verification = verification_record(input_data)
    command = command_from_input(input_data)
    # Citation-verification activity: what this call actually read/fetched/ran.
    # Only record when the call did NOT observably fail (failure is None means
    # success or unknown) -- so a failed `grep missing.py` or a non-zero check is
    # never credited as real activity backing a citation.
    executed_ok = failure is None
    reads = [_abs(p, cwd) for p in read_targets(input_data)] if executed_ok else []
    fetched = fetched_url_targets(input_data) if executed_ok else []
    ran = ran_command(input_data) if executed_ok else None

    def apply(ledger):
        if kinds:
            ledger["changed_files_seen"] = True
            add_unique(ledger, "change_kinds", kinds)
        if verification:
            ledger["verification_results"].append(verification)
            if command:
                ledger["verification_commands"].append(verification["command"])
        if failure:
            ledger["failures"].append(failure)
        if reads:
            add_unique(ledger, "read_paths", reads)
        if fetched:
            add_unique(ledger, "fetched_urls", fetched)
        if ran:
            add_unique(ledger, "ran_commands", [ran])

    ledger = update_ledger(input_data, apply)

    # silent-recovery guard: recover quietly from one-offs, but if the SAME class
    # of failure repeats (>=2), disclose it instead of retrying silently.
    repeat = repeated_failure(ledger.get("failures", [])) if failure else None
    if repeat:
        _sig, count = repeat
        emit_json(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"unifable: the same class of failure has repeated {count} times. "
                        "Stop retrying silently — report it briefly (what failed / recovery "
                        "already tried / next path)."
                    ),
                }
            }
        )
    elif failure:
        emit_json(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "unifable gate observed a tool failure. Do not report completion until it is fixed, isolated as a known baseline, or explicitly documented.",
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
        emit_json({"systemMessage": f"unifable gate post-tool hook failed open: {exc}"})
        raise SystemExit(0)
