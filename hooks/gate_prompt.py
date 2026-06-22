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
from spec import canonical_project_root, resolve_session_id, save_spec, spec_path, spec_template


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
            s["restated_goal"] = _seed_goal(prompt)  # placeholder; agent must restate
            s["goal_seeded"] = True  # gate stays blocked until `spec.py restate` clears this
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
    cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))
    # The evidence spec is now ONE per (directory, session) -- keyed by the session,
    # not the prompt -- so a new session never inherits a prior one's spec. The
    # per-prompt hash still feeds the groundedness breaker's debounce key, so keep
    # it in `active_task`; it no longer keys the spec.
    new_key = _prompt_key(prompt)
    session_key = resolve_session_id(input_data, default="default") or "default"

    def apply(ledger):
        # `active_task` = the current prompt hash, for the breaker debounce only.
        # There is one session spec, so a new prompt cannot escape the gate by
        # re-pointing a key -- the spec is found by session, not by this value.
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
        key = session_key  # one spec per session; CLI resolves session from host env
        path = _ensure_spec_scaffold(cwd, key, prompt)
        if path:
            context += (
                f"\n\nunifable: evidence spec auto-created at {path}. Drive it via the "
                f"append-only CLI (never edit the JSON):\n"
                f"  - FIRST, restate the goal in your own words (what is the intended outcome?): "
                f"unifable-spec restate --goal '<your restatement>' "
                f"(the seeded goal is the raw prompt; the gate stays blocked until you restate)\n"
                f"  - add a requirement: unifable-spec add-task "
                f"--title '<requirement>' --check '<runnable check>'\n"
                f"  - add evidence: unifable-spec cite "
                f"--repo-context 'path:line::why' --prior-art '<url>::why'\n"
                f"  - submit a requirement: unifable-spec deliver --task <id>; "
                f"then validate-task --task <id> (runs the check, then the judge reviews the output)\n"
                f"  - if a requirement is genuinely impossible: unifable-spec dispute "
                f"--task <id> --evidence '<proof>' (the judge adjudicates; only it can retract)"
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
