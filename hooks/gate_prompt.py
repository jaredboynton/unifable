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

from ledger import add_unique, emit_json, load_ledger, read_stdin_json, update_ledger
from classify_task import operative_prompt, context_for_mode, grade_of
from evidence_policy import higher_mode, mode_for_grade, resolve_grade
from grade_override import judge_grade_classify, parse_grade_verdict, _task_summary
from heavy_workflow import heavy_workflow_brief
from spec import canonical_project_root, load_spec, resolve_session_id, save_spec, spec_path, spec_template


def _prompt_key(prompt: str) -> str:
    """Stable per-task key = sha256(prompt) prefix. Specs are keyed by this, so a
    distinct prompt seeds a distinct spec (multiple specs per session)."""
    return hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()[:16]


def _seed_goal(prompt: str, limit: int = 280) -> str:
    """Best-effort restated_goal for the scaffold: the trimmed prompt. The agent
    refines it; the gate only requires a non-empty string."""
    g = " ".join((prompt or "").split())
    return g[:limit]


def _ensure_spec_scaffold(cwd: str, key: str, prompt: str, *, heavy: bool = False) -> str:
    """Auto-create the evidence spec (the agent never runs `create`). Writes a
    scaffold with `requires_tasks` so an empty spec is not completable, seeds the
    goal from the prompt, and returns the spec path for injection. Fail-open:
    returns "" on any error and never raises into the hook."""
    try:
        path = spec_path(cwd, key)
        if not path.exists():
            s = spec_template()
            s["restated_goal"] = _seed_goal(prompt)
            s["goal_seeded"] = True  # gate blocked until `unifable restate '<goal>'`
            s["acceptance_criteria"] = []
            s["repo_context"] = []
            s["prior_art"] = []
            s["tasks"] = []
            s["requires_tasks"] = True  # empty spec must gain >=1 requirement to complete
            if heavy:
                s["heavy_workflow"] = True
            save_spec(cwd, key, s)
        elif heavy:
            s = load_spec(cwd, key)
            if isinstance(s, dict) and not s.get("heavy_workflow"):
                s["heavy_workflow"] = True
                save_spec(cwd, key, s)
        return str(path)
    except Exception:
        return ""


def main() -> int:
    try:
        from cli_install import ensure_cli

        ensure_cli()
    except Exception:
        pass

    input_data = read_stdin_json()
    prompt = str(input_data.get("prompt") or input_data.get("user_prompt") or "")
    cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))
    new_key = _prompt_key(prompt)
    session_key = resolve_session_id(input_data, default="default") or "default"

    # Judge-backed grade classification (single call per prompt). Fails open to
    # normal/STANDARD on any judge/transport error.
    operative = operative_prompt(prompt)
    prior_spec = None
    try:
        prior_spec = load_spec(cwd, session_key)
    except Exception:
        pass
    restated = str(prior_spec.get("restated_goal") or "") if isinstance(prior_spec, dict) else ""
    task_summary = _task_summary(prior_spec) if isinstance(prior_spec, dict) else None
    verdict = judge_grade_classify(operative, restated_goal=restated, task_summary=task_summary)
    mode, risks, reason = parse_grade_verdict(verdict)
    if verdict is None:
        mode, risks, reason = "normal", [], "judge unavailable: classified as normal (fail-open)"
    grade = grade_of(mode)

    def apply(ledger):
        prior_mode = (ledger.get("task_mode") or "").lower().strip()
        ledger["active_task"] = new_key
        pinned_target = (
            ledger.get("grade_override_target")
            if ledger.get("grade_override_applied")
            else None
        )
        if pinned_target:
            ledger["task_mode"] = mode_for_grade(str(pinned_target))
        else:
            ledger["task_mode"] = higher_mode(prior_mode, mode) if prior_mode else mode
        ledger["grade"] = grade_of(ledger["task_mode"])
        ledger["warning_count"] = 0
        ledger["warnings"] = []
        ledger["changed_files_seen"] = False
        ledger["change_kinds"] = []
        ledger["risk_flags"] = []
        ledger["verification_commands"] = []
        ledger["verification_results"] = []
        ledger["failures"] = []
        ledger["stop_blocks"] = 0
        ledger["frontier_discovery_count"] = ledger.get("frontier_discovery_count", 0)
        add_unique(ledger, "risk_flags", risks)
        effective = resolve_grade(ledger)
        if effective == "HEAVY" and not ledger.get("heavy_brief_injected"):
            ledger["heavy_brief_injected"] = True
            ledger["inject_heavy_brief"] = True
        else:
            ledger["inject_heavy_brief"] = False

    update_ledger(input_data, apply)

    ledger = load_ledger(input_data)
    effective_grade = resolve_grade(ledger)
    heavy_scaffold = effective_grade == "HEAVY"

    context = context_for_mode(mode, risks)

    if effective_grade != "LIGHT":
        key = session_key
        path = _ensure_spec_scaffold(cwd, key, prompt, heavy=heavy_scaffold)
        if path:
            context += (
                f"\n\nunifable: evidence spec auto-created at {path}. "
                f"Drive it via the append-only CLI (never edit the JSON):\n"
                f"  - FIRST: unifable restate '<your restatement of the intended outcome>' "
                f"(the seeded goal is the raw prompt; the gate stays blocked until you restate)\n"
                f"  - unifable add-task --title '<requirement>' --check '<runnable check>'\n"
            )
            if heavy_scaffold:
                context += (
                    f"  - HEAVY: unifable set-primary --title '...' --check '...'\n"
                    f"  - HEAVY: unifable add-frontier --title '...' --check '...' (>=2; judge may auto-add)\n"
                )
            context += (
                f"  - if a requirement is genuinely impossible: unifable dispute "
                f"--task <id> --evidence '<proof>' (the judge adjudicates on stop; only it can retract)\n"
                f"Citations sync from your reads/fetches automatically; checks run on stop."
            )

    try:
        if ledger.get("inject_heavy_brief"):
            context += "\n\n" + heavy_workflow_brief()
    except Exception:
        if heavy_scaffold:
            context += "\n\n" + heavy_workflow_brief()

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
