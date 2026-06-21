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
from spec import all_tasks_validated, load_spec, save_spec, spec_path, spec_template


def _prompt_key(prompt: str) -> str:
    """Stable per-task key = sha256(prompt) prefix. Specs are keyed by this, so a
    distinct prompt seeds a distinct spec (multiple specs per session)."""
    return hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()[:16]


def _seed_goal(prompt: str, limit: int = 280) -> str:
    """Best-effort restated_goal for the scaffold: the trimmed prompt. The agent
    refines it; the gate only requires a non-empty string."""
    g = " ".join((prompt or "").split())
    return g[:limit]


def _ensure_spec_scaffold(cwd: str, key: str, prompt: str) -> str:
    """Auto-create the evidence spec (the agent never runs `create`). Writes a
    scaffold with `requires_tasks` so an empty spec is not completable, seeds the
    goal from the prompt, and returns the spec path for injection. Fail-open:
    returns "" on any error and never raises into the hook."""
    try:
        path = spec_path(cwd, key)
        if not path.exists():
            s = spec_template()
            s["restated_goal"] = _seed_goal(prompt)
            s["acceptance_criteria"] = []
            s["repo_context"] = []
            s["prior_art"] = []
            s["tasks"] = []
            s["requires_tasks"] = True  # empty spec must gain >=1 requirement to complete
            save_spec(cwd, key, s)
        return str(path)
    except Exception:
        return ""


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

    ledger = update_ledger(input_data, apply)

    context = context_for_mode(mode, risks)

    # Auto-create the evidence spec on the hook path for non-trivial work, and tell
    # the agent how to drive it (append-only: add requirements + evidence; dispute
    # the impossible; never edit the JSON). LIGHT work is waived (no spec).
    if grade_of(mode) != "LIGHT":
        key = ledger.get("active_task") or new_key
        path = _ensure_spec_scaffold(cwd, key, prompt)
        if path:
            context += (
                f"\n\nunifable: evidence spec auto-created at {path}. Drive it via the "
                f"append-only CLI (never edit the JSON):\n"
                f"  - add a requirement: python3 scripts/gate/spec.py add-task --task-id {key} "
                f"--title '<requirement>' --check '<runnable check>'\n"
                f"  - add evidence: python3 scripts/gate/spec.py cite --task-id {key} "
                f"--repo-context 'path:line::why' --prior-art '<url>::why'\n"
                f"  - submit a requirement: python3 scripts/gate/spec.py deliver --task-id {key} --task <id>; "
                f"then validate-task --task-id {key} --task <id> (the judge decides)\n"
                f"  - if a requirement is genuinely impossible: python3 scripts/gate/spec.py dispute "
                f"--task-id {key} --task <id> --evidence '<proof>' (the judge adjudicates; only it can retract)"
            )

    emit_json(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
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
