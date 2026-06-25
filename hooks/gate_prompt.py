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

from classify_task import context_for_mode, grade_of, operative_prompt
from evidence_policy import mode_for_grade, resolve_grade
from grade_override import _task_summary, judge_grade_classify, parse_grade_verdict
from heavy_workflow import heavy_workflow_brief
from ledger import add_unique, emit_json, load_ledger, read_stdin_json, update_ledger
from plan_mode import (
    mark_plan_mode_prompt_notified,
    plan_mode_context_line,
    plan_mode_prompt_line_needed,
    plan_mode_spec_task_guidance,
    resolve_plan_mode,
)
from spec_io import canonical_project_root, ensure_spec_scaffold, load_spec, resolve_session_id
from task_context import self_referential_harness_context_line


def _prompt_key(prompt: str) -> str:
    """Stable per-task key = sha256(prompt) prefix. Specs are keyed by this, so a
    distinct prompt seeds a distinct spec (multiple specs per session)."""
    return hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()[:16]


def _format_scaffold_onboarding(
    path: str,
    *,
    evidence_profile: str,
    heavy_scaffold: bool,
    plan_mode: dict,
    skip_cli_tutorial: bool = False,
) -> str:
    """Full spec CLI tutorial — emit only on first scaffold create."""
    profile_note = (
        " Operational profile: no repo path:line or external URL required before edits."
        if evidence_profile == "operational"
        else ""
    )
    if skip_cli_tutorial:
        return f"\n\nEvidence spec auto-created at {path}.{profile_note}"
    task_guidance = ""
    try:
        task_guidance = plan_mode_spec_task_guidance(plan_mode)
    except Exception:
        pass
    block = (
        f"\n\nEvidence spec auto-created at {path}.{profile_note} "
        f"Drive it via the append-only CLI (never edit the JSON); the director judge guides each step from there:\n"
        f"  - FIRST: unifable restate '<the intended outcome, in your own words>' (the gate stays blocked until you restate)\n"
        f"  - unifable add-task --title '<requirement>' --check '<runnable check>'"
        f"{task_guidance}\n"
    )
    if heavy_scaffold:
        block += (
            "  - HEAVY: unifable set-primary / add-frontier (>=2 frontiers; judge may auto-add and picks the best on stop)\n"
        )
    block += (
        "  - if a requirement is genuinely impossible: unifable dispute --task <id> --evidence '<proof>' "
        "(only the judge can retract). Citations sync from your reads automatically."
    )
    return block


def main() -> int:
    try:
        from runtime_sync import sync_runtime

        sync_runtime()
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
    # Only feed the task board to the judge for substantive prompts. Short
    # continuations ("proceed", "continue") have almost no operative signal, so
    # board noise (stale/speculative task titles) can pollute the classification.
    task_summary = None
    if len(operative.split()) >= 20:
        task_summary = _task_summary(prior_spec) if isinstance(prior_spec, dict) else None
    verdict = judge_grade_classify(operative, restated_goal=restated, task_summary=task_summary)
    mode, risks, reason, evidence_profile = parse_grade_verdict(verdict)
    if verdict is None:
        mode, risks, reason, evidence_profile = (
            "normal",
            [],
            "judge unavailable: classified as normal (fail-open)",
            "code",
        )
    grade = grade_of(mode)

    def apply(ledger):
        ledger["active_task"] = new_key
        ledger["evidence_profile"] = evidence_profile
        pinned_target = ledger.get("grade_override_target") if ledger.get("grade_override_applied") else None
        if pinned_target:
            ledger["task_mode"] = mode_for_grade(str(pinned_target))
        else:
            # The judge classification is authoritative per-prompt. No
            # higher_mode stickiness: a bounded "proceed" correctly drops from
            # HEAVY to STANDARD when the judge says normal.
            ledger["task_mode"] = mode
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
        ledger["pretool_block_epoch"] = ""
        ledger["pretool_block_counts"] = {}
        ledger["frontier_discovery_count"] = ledger.get("frontier_discovery_count", 0)
        pm = resolve_plan_mode(input_data, transcript_path=input_data.get("transcript_path"))
        ledger["plan_mode_enabled"] = bool(pm.get("enabled"))
        ledger["plan_mode_host"] = str(pm.get("host") or "")
        add_unique(ledger, "risk_flags", risks)
        effective = resolve_grade(ledger)
        if effective == "HEAVY" and not ledger.get("heavy_brief_injected"):
            ledger["heavy_brief_injected"] = True
            ledger["inject_heavy_brief"] = True
        else:
            ledger["inject_heavy_brief"] = False

    prior_ledger = load_ledger(input_data)
    prior_grade = resolve_grade(prior_ledger)
    prior_profile = str(prior_ledger.get("evidence_profile") or "")
    prior_active = bool(prior_ledger.get("active_task"))
    update_ledger(input_data, apply)

    ledger = load_ledger(input_data)
    effective_grade = resolve_grade(ledger)
    heavy_scaffold = effective_grade == "HEAVY"

    context = context_for_mode(
        mode,
        risks,
        first_prompt=not prior_ledger.get("citation_footer_notified"),
    )

    if not prior_ledger.get("citation_footer_notified") and "Cite evidence" in context:
        try:

            def _mark_cite_footer(_led):
                _led["citation_footer_notified"] = True

            update_ledger(input_data, _mark_cite_footer)
        except Exception:
            pass

    try:
        sr_line = self_referential_harness_context_line(operative)
        if sr_line:
            context += sr_line
    except Exception:
        pass

    try:
        plan_mode = resolve_plan_mode(input_data, transcript_path=input_data.get("transcript_path"))
        if plan_mode_prompt_line_needed(input_data, plan_mode):
            plan_line = plan_mode_context_line(plan_mode)
            if plan_line:
                context += plan_line
                mark_plan_mode_prompt_notified(input_data)
    except Exception:
        plan_mode = {"enabled": False}

    # Gap 3: when the per-prompt classification shifts the enforcement grade or the
    # evidence profile, surface the judge's reason and the move -- the generic mode
    # line above does not tell the model the gate's requirements just changed.
    if prior_active and (effective_grade != prior_grade or evidence_profile != prior_profile):
        detail = (reason or "").strip() or "requirements changed"
        line = f"\n\nReclassified: {detail}"
        if effective_grade != prior_grade:
            line += f" Enforcement is now {effective_grade} (was {prior_grade})."
        if evidence_profile != prior_profile:
            if evidence_profile == "operational":
                line += " Repo citations not required before edits."
            else:
                line += " Repo and prior-art citations required before edits."
        context += line

    if effective_grade != "LIGHT":
        key = session_key
        path, scaffold_changes, scaffold_created = ensure_spec_scaffold(
            cwd, key, prompt, heavy=heavy_scaffold, evidence_profile=evidence_profile
        )
        if path and scaffold_created and not ledger.get("prompt_scaffold_notified"):
            context += _format_scaffold_onboarding(
                path,
                evidence_profile=evidence_profile,
                heavy_scaffold=heavy_scaffold,
                plan_mode=plan_mode if isinstance(plan_mode, dict) else {},
                skip_cli_tutorial=bool(ledger.get("inject_heavy_brief")),
            )

            def _mark_scaffold(_led):
                _led["prompt_scaffold_notified"] = True

            update_ledger(input_data, _mark_scaffold)
        elif path and scaffold_changes:
            context += "\n\nSpec scaffold updated: " + "; ".join(scaffold_changes) + "."

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
        emit_json({"systemMessage": f"Gate prompt hook failed open: {exc}"})
        raise SystemExit(0)
