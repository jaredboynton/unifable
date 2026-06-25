#!/usr/bin/env python3
"""Per-grade spec contract strings and model-facing validation blocks (unifable).

Builds the additionalContext pass-conditions the hooks surface and the failure
block shown when validate_spec rejects a spec. Re-exported by the spec.py facade.
"""

from __future__ import annotations

from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from spec_schema import GRADES
    from spec_validation import repo_maintenance_waives_prior_art
except ImportError:  # pragma: no cover
    from scripts.gate.spec_validation import repo_maintenance_waives_prior_art


_CONTRACT: dict[str, str] = {
    "LIGHT": (
        "Spec contract — LIGHT grade. "
        "Before editing: drive the auto-created spec via the spec.py CLI so it carries'restated_goal' (non-empty string) "
        "and 'acceptance_criteria' (list with >=1 {check, evidence} entry). "
        "Evidence must be live command output — no placeholders."
    ),
    "STANDARD": (
        "Spec contract — STANDARD grade. "
        "Before editing: drive the auto-created spec via the spec.py CLI so it carries'restated_goal' "
        "and 'acceptance_criteria' (>=1 {check: <runnable command>, evidence: <live output>}). "
        "Evidence must be observed tool output, not assumed."
    ),
    "HEAVY": (
        "Spec contract — HEAVY grade (frontier-first workflow with adoption). "
        "Before editing: restated_goal, citation evidence, >=2 frontier approach tasks, "
        "and 1 primary approach task. Judge adjudicates frontiers on Stop "
        "(rejected_approach / still_viable / accepted_approach). When all frontiers "
        "are explored, the judge compares evidence and may adopt the best frontier "
        "(primary is superseded) or fall back to primary if none accepted. "
        "CLI: unifable set-primary / unifable add-frontier."
    ),
}


def contract_string(
    grade: str,
    require_evidence: bool = False,
    evidence_profile: str | None = None,
    spec: dict[str, Any] | None = None,
) -> str:
    """Return the pass-conditions for *grade* as a short additionalContext string.

    When *require_evidence* is True, append the evidence-gate citation requirements
    (code profile: repo_context + prior_art at STANDARD+; operational: tasks only).
    """
    try:
        from evidence_policy import DEFAULT_EVIDENCE_PROFILE, resolve_evidence_profile
    except ImportError:  # pragma: no cover
        from scripts.gate.evidence_policy import DEFAULT_EVIDENCE_PROFILE

    grade = (grade or "STANDARD").upper()
    base = _CONTRACT.get(grade, _CONTRACT["STANDARD"])
    profile = (evidence_profile or DEFAULT_EVIDENCE_PROFILE).lower().strip()
    if profile not in ("code", "operational"):
        profile = DEFAULT_EVIDENCE_PROFILE
    if require_evidence and grade != "LIGHT":
        if profile == "operational":
            base = base + (
                " Evidence gate (operational): restated goal + >=1 requirement task; "
                "no repo path:line or external URL required before edits -- task "
                "checks are judged at Stop."
            )
        else:
            if isinstance(spec, dict) and repo_maintenance_waives_prior_art(spec):
                base = base + (
                    " Evidence gate (in-repo): include 'repo_context' (>=1 "
                    "{cite:'path:line', why:'why it's relevant'}) from code you read; "
                    "external prior_art is not required for bounded in-repo work "
                    "(maintenance, regression tests, patterns from existing code)."
                )
            else:
                base = base + (
                    " Evidence gate: also include 'repo_context' (>=1 {cite:'path:line', why:'why it's "
                    "relevant'}) and 'prior_art' (>=1 {cite:'http(s)://...', why:'why it backs the approach'})."
                )
    return base


def format_spec_validation_block(
    grade: str,
    reasons: list[str],
    evidence_profile: str | None = None,
    spec: dict[str, Any] | None = None,
    *,
    include_contract: bool = True,
    scaffold_notified: bool = False,
    contract_notified: bool = False,
) -> str:
    """Model-facing block text when validate_spec fails.

    Omits filesystem paths (the model drives the spec via CLI and activity sync,
    not by editing spec.json). Appends concrete fix steps derived from *reasons*.
    """
    grade = (grade or "STANDARD").upper()
    items = [str(r).strip() for r in (reasons or []) if str(r).strip()]
    joined = " ".join(items).lower()
    fixes: list[str] = []

    if "prior_art" in joined:
        fixes.append(
            "fetch at least one relevant source URL (WebFetch or curl); prior_art entries sync from fetched URLs automatically"
        )
    if "repo_context" in joined:
        fixes.append("read relevant repo files (Read/Grep); repo_context entries sync from reads automatically")
    if "restate" in joined or "restated_goal" in joined or "goal_seeded" in joined:
        fixes.append("run `unifable restate '<goal in your own words>'`")
    if "no requirements yet" in joined or "requires_tasks" in joined:
        fixes.append("run `unifable add-task --title '<requirement>' --check '<runnable check>'`")
    if grade == "HEAVY" and ("frontier" in joined or "primary approach" in joined):
        fixes.append("HEAVY: use `unifable add-frontier` (>=2) and `unifable set-primary`")

    joined_text = " ".join(items).lower()
    fixes = [fix for fix in fixes if fix.lower() not in joined_text and not _fix_in_reasons(fix, items)]

    if contract_notified:
        include_contract = False

    compact = scaffold_notified or contract_notified
    if compact:
        lines: list[str] = []
        for item in items:
            line = item.rstrip(".")
            if line:
                lines.append(line)
        if fixes:
            for fix in fixes:
                if fix not in lines:
                    lines.append(fix)
        if include_contract:
            lines.append("")
            lines.append(contract_string(grade, True, evidence_profile, spec))
        return "\n".join(lines).strip()

    lines = [f"Evidence spec does not satisfy grade {grade}:"]
    lines.extend(f"  {item}" for item in items)
    if fixes:
        lines.append("")
        lines.append("To unblock edits:")
        lines.extend(f"  {fix}" for fix in fixes)
    else:
        lines.append("")
        lines.append("Fix the spec via the unifable CLI (never edit spec.json directly).")
    if include_contract:
        lines.append("")
        lines.append(contract_string(grade, True, evidence_profile, spec))
    return "\n".join(lines)


def _fix_in_reasons(fix: str, reasons: list[str]) -> bool:
    fix_low = fix.lower()
    for reason in reasons:
        reason_low = reason.lower()
        if fix_low in reason_low:
            return True
        if "restate" in fix_low and ("restate" in reason_low or "restated_goal" in reason_low):
            return True
        if "add-task" in fix_low and "add-task" in reason_low:
            return True
    return False
