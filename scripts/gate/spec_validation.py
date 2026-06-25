#!/usr/bin/env python3
"""Evidence-spec validation and prior-art waiver rules (unifable).

`validate_spec` is the predicate the pre-edit and completion gates run against the
spec artifact. Host-agnostic; re-exported by the spec.py facade.
"""

from __future__ import annotations

import re
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from heavy_workflow import frontier_tasks, primary_task, sync_heavy_phase
except ImportError:  # pragma: no cover
    from scripts.gate.heavy_workflow import frontier_tasks, primary_task, sync_heavy_phase

try:
    from spec_schema import (
        GRADES,
        check_fake_evidence,
        is_path_line,
        is_source_url,
        prior_art_parts,
        repo_context_of,
        repo_context_parts,
    )
except ImportError:  # pragma: no cover
    from scripts.gate.spec_schema import (
        GRADES,
        check_fake_evidence,
        is_path_line,
        is_source_url,
        prior_art_parts,
        repo_context_of,
        repo_context_parts,
    )


_REPO_MAINTENANCE_RE = re.compile(
    r"\b("
    r"version\s+bump|bump\s+version|just\s+version|bump_version|plugin\.json|marketplace\.json|"
    r"setup/setup\.sh|setup\.sh|manifest\s+sync|pre-commit|release(?:\s+tail|\s+workflow|\s+number)?|"
    r"tag\s+and\s+push|plugin\s+manifest"
    r")\b",
    re.I,
)


_IN_REPO_WORK_RE = re.compile(
    r"\b("
    r"regression\s+test|"
    r"add(?:\s+an?|\s+the)?\s+(?:focused\s+|unit\s+|integration\s+)?tests?\b|"
    r"tests?/test_|"
    r"\bpytest\b|"
    r"test\s+(?:coverage|harness|suite)|"
    r"in-repo|in\s+this\s+repo|within\s+(?:the\s+)?repo|this\s+codebase|"
    r"follow(?:ing)?\s+(?:the\s+)?existing\s+(?:test|pattern|convention)s?|"
    r"extend(?:ing)?\s+(?:the\s+)?existing\s+test"
    r")\b",
    re.I,
)


_EXTERNAL_RESEARCH_RE = re.compile(
    r"\b("
    r"api\s+doc|external\s+api|third.?party|platform\s+behavior|undocumented\s+endpoint|"
    r"greenfield|new\s+architecture|migration\s+from\s+scratch"
    r")\b",
    re.I,
)


def repo_maintenance_waives_prior_art(spec: dict[str, Any]) -> bool:
    """True when prior_art is not required for bounded in-repo work.

    Covers repo maintenance (version bump, manifest sync) and engineering that
    follows existing in-repo patterns (regression tests, test additions, harness
    self-tests). External-research signals in the goal or tasks override the waiver.
    """
    if not isinstance(spec, dict):
        return False
    chunks: list[str] = [str(spec.get("restated_goal") or "")]
    for task in spec.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        chunks.append(str(task.get("title") or ""))
        chunks.append(str(task.get("check") or ""))
    combined = "\n".join(chunks)
    if _EXTERNAL_RESEARCH_RE.search(combined):
        return False
    return bool(_REPO_MAINTENANCE_RE.search(combined) or _IN_REPO_WORK_RE.search(combined))


def validate_spec(
    spec: dict[str, Any],
    grade: str,
    require_evidence: bool = False,
    evidence_profile: str | None = None,
) -> tuple[bool, list[str]]:
    """Validate *spec* against the requirements for *grade*.

    When *require_evidence* is True (how the hooks always call it), the spec must
    also carry citation evidence at STANDARD+ for the *code* profile: 'repo_context'
    (>=1 {cite: 'path:line', why: '<why relevant>'}) and 'prior_art' (>=1
    {cite: 'http(s)://...', why: '<why relevant>'}). The *operational* profile
    waives both at STANDARD+; evidence is task-driven and judged at Stop.

    Returns (ok, reasons) where reasons is empty when ok is True.
    """
    try:
        from evidence_policy import resolve_evidence_profile
    except ImportError:  # pragma: no cover
        from scripts.gate.evidence_policy import resolve_evidence_profile
    grade = (grade or "STANDARD").upper()
    if grade not in GRADES:
        return False, [f"Unknown grade '{grade}'; expected one of {', '.join(GRADES)}."]

    profile = resolve_evidence_profile(spec=spec if isinstance(spec, dict) else None)
    if evidence_profile is not None:
        profile = (evidence_profile or "").lower().strip() or profile

    reasons: list[str] = []

    if not isinstance(spec, dict):
        return False, ["Spec must be a JSON object."]

    # restated_goal — required for all grades. The auto-creation hook seeds it with
    # the raw prompt and marks `goal_seeded`; that verbatim copy is a placeholder,
    # not a restatement. The agent must rewrite it in its own words (thinking about
    # the intended outcome) via `spec.py restate`, which clears the marker.
    goal = spec.get("restated_goal")
    if not goal or not isinstance(goal, str) or not goal.strip():
        reasons.append("'restated_goal' is required and must be a non-empty string.")
    elif spec.get("goal_seeded"):
        reasons.append(
            "restate the goal in your own words first: restated_goal is still the raw "
            "prompt the hook seeded, not a restatement. Run "
            "`unifable restate '<the intended outcome, in your own words>'`."
        )

    # acceptance_criteria — required for all grades, >=1 item with a non-empty check.
    # A task-spec (CLI-authored, has >=1 task with a check) satisfies this instead:
    # the tasks ARE the acceptance criteria, and their live evidence is produced at
    # validate-task time (judged), not at authoring time.
    tasks = spec.get("tasks")
    has_tasks = isinstance(tasks, list) and any(isinstance(t, dict) and str(t.get("check", "")).strip() for t in tasks)
    criteria = spec.get("acceptance_criteria")
    if has_tasks:
        pass  # tasks stand in for acceptance_criteria
    elif spec.get("requires_tasks"):
        # Auto-created task-spec with no requirement yet: the agent must add >=1.
        reasons.append(
            "no requirements yet: add at least one with `unifable add-task --title '<req>' --check '<runnable check>'`."
        )
    elif not isinstance(criteria, list) or not criteria:
        reasons.append("'acceptance_criteria' is required and must contain at least one entry.")
    else:
        for idx, item in enumerate(criteria):
            if not isinstance(item, dict):
                reasons.append(f"acceptance_criteria[{idx}] must be an object with 'check' and 'evidence' keys.")
                continue
            check = item.get("check", "")
            if not isinstance(check, str) or not check.strip():
                reasons.append(f"acceptance_criteria[{idx}].check must be a non-empty runnable command string.")
            evidence = item.get("evidence", "")
            if not isinstance(evidence, str) or not evidence.strip():
                reasons.append(f"acceptance_criteria[{idx}].evidence must be a non-empty string.")
            else:
                fakes = check_fake_evidence(evidence)
                if fakes:
                    reasons.append(
                        f"acceptance_criteria[{idx}].evidence is an unproven assumption/placeholder "
                        f"({fakes}). The gate rejects assumptions -- prove it: paste live output "
                        "(cmd -> output), a code citation (path:line), or a source URL."
                    )

    # HEAVY: frontier-first approach workflow (constraints removed)
    if grade == "HEAVY":
        sync_heavy_phase(spec)
        n_frontier = len(frontier_tasks(spec))
        if n_frontier < 2:
            reasons.append(
                f"HEAVY grade requires >=2 frontier approach tasks "
                f"(have {n_frontier}); use `unifable add-frontier` or wait for judge discovery."
            )
        primary = primary_task(spec)
        if primary is None:
            reasons.append(
                "HEAVY grade requires exactly 1 primary approach task "
                "(evidence-backed fallback); use `unifable set-primary --title ... --check ...`."
            )
        elif not str(primary.get("title") or "").strip() or not str(primary.get("check") or "").strip():
            reasons.append("HEAVY primary approach task must have non-empty title and check.")

    # Evidence gate: citation fields become required at STANDARD+ for code-profile
    # tasks (LIGHT is exempt because LIGHT waives the spec entirely upstream).
    # Operational profile waives repo_context and prior_art; task checks carry evidence.
    if require_evidence and grade in ("STANDARD", "HEAVY") and profile == "code":
        repo_context = repo_context_of(spec)  # accepts legacy `must_read` alias
        if not repo_context:
            reasons.append(
                "evidence gate: 'repo_context' is required (list, >=1 {cite: 'path:line', why: 'why this passage is relevant'})."
            )
        else:
            for idx, item in enumerate(repo_context):
                cite, why = repo_context_parts(item)
                if not is_path_line(cite):
                    reasons.append(
                        f"repo_context[{idx}].cite must be a 'path:line' code citation (e.g. src/app.py:42), got {item!r}."
                    )
                elif check_fake_evidence(cite):
                    reasons.append(
                        f"repo_context[{idx}].cite is an unproven assumption/placeholder ({cite!r}). "
                        "The gate rejects assumptions -- cite a real path:line you read."
                    )
                if not why.strip():
                    reasons.append(
                        f"repo_context[{idx}] needs a non-empty 'why' (why the passage is relevant); "
                        f"use {{'cite': '{cite or 'path:line'}', 'why': '...'}}."
                    )
                elif check_fake_evidence(why):
                    reasons.append(
                        f"repo_context[{idx}].why is an unproven assumption/placeholder ({why!r}). "
                        "The gate rejects assumptions -- prove why the passage is relevant."
                    )

        # prior_art — required from STANDARD up unless bounded in-repo work at STANDARD
        # (maintenance, regression tests, existing-pattern edits) where repo_context suffices.
        # HEAVY always requires prior_art for architectural exploration.
        waive_prior_art = grade != "HEAVY" and repo_maintenance_waives_prior_art(spec)
        if not waive_prior_art:
            prior_art = spec.get("prior_art")
            if not isinstance(prior_art, list) or not prior_art:
                reasons.append(
                    "evidence gate: 'prior_art' is required (list, >=1 "
                    "{cite: 'http(s)://...', why: 'why this source backs the approach'})."
                )
            else:
                for idx, item in enumerate(prior_art):
                    cite, why = prior_art_parts(item)
                    if not is_source_url(cite):
                        reasons.append(f"prior_art[{idx}].cite must be a source URL (http(s)://...), got {item!r}.")
                    if not why.strip():
                        reasons.append(
                            f"prior_art[{idx}] needs a non-empty 'why' (why this source backs the "
                            f"approach); use {{'cite': '{cite or 'http(s)://...'}', 'why': '...'}}."
                        )
                    elif check_fake_evidence(why):
                        reasons.append(
                            f"prior_art[{idx}].why is an unproven assumption/placeholder ({why!r}). "
                            "The gate rejects assumptions -- prove why this source is relevant."
                        )

    return not reasons, reasons
