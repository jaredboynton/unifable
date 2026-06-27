#!/usr/bin/env python3
"""unifable observation gate — PostToolUse.

Records observed evidence after each Bash/Edit/Write tool call: whether files
changed (and their kind), and whether a verification command ran and observably
succeeded or failed. While the groundedness breaker is armed, runs the release
judge after Read/WebFetch-style tools and injects breaker-open context when the
claim is grounded. Spec CLI notifications are forwarded to the model. Fails open.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

from ledger import add_unique, emit_json, load_ledger, read_stdin_json, update_ledger
from model_notify import (
    build_posttool_spec_context,
    is_spec_cli_command,
)
from parse_tool_result import (
    changed_kinds,
    command_from_input,
    command_output_evidence,
    detect_failure,
    fetched_url_targets,
    mcp_evidence,
    ran_command,
    read_targets,
    repeated_failure,
    research_bash_evidence,
    response_text,
    verification_record,
)
from posttool_notify import emit_posttool_context, prepare_posttool_parts
from spec_io import canonical_project_root


def _abs(path: str, cwd: str) -> str:
    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path(cwd) / p
        return str(p.resolve())
    except (OSError, ValueError):
        return path


def _fresh_tool_block(input_data: dict, tool_name: str, executed_ok: bool) -> str:
    if not executed_ok:
        return ""
    excerpt = response_text(input_data.get("tool_response", input_data), 4000)
    if not excerpt:
        return ""
    return f"[tool_result name={tool_name}]\n{excerpt}"


def _spec_context(input_data: dict, tool_name: str, cwd: str) -> tuple[str, dict[str, dict[str, str]] | None]:
    if tool_name != "Bash":
        return "", None
    command = command_from_input(input_data)
    if not is_spec_cli_command(command):
        return "", None
    try:
        from spec_io import load_spec, resolve_session_id

        task_id = resolve_session_id(input_data, default=None)
        spec = load_spec(cwd, task_id) if task_id else None
        ledger = load_ledger(input_data)
        return build_posttool_spec_context(
            command,
            input_data.get("tool_response", input_data),
            spec,
            ledger,
        )
    except Exception:
        return "", None


def _breaker_release_context(input_data: dict, tool_name: str, executed_ok: bool) -> str:
    try:
        from breaker_orchestration import evaluate_post_tool_release
        from breaker_runtime import is_release_tool
        from breaker_state import load_breaker, save_breaker

        if not executed_ok or not is_release_tool(tool_name, input_data):
            return ""
        breaker = load_breaker(input_data)
        if not breaker.get("breaker_armed") and not breaker.get("breaker_provisional"):
            return ""
        fresh = _fresh_tool_block(input_data, tool_name, executed_ok)
        if not fresh:
            return ""
        from ledger import load_ledger

        ledger = load_ledger(input_data)
        active_task = str(ledger.get("active_task") or "")
        _grounded, _needed, message = evaluate_post_tool_release(input_data, breaker, fresh_tool=fresh, active_task=active_task)
        save_breaker(input_data, breaker)
        return message
    except Exception:
        return ""


def _repeated_failure_hint(input_data: dict, ledger: dict, cwd: str, count: int) -> str:
    """Advisory nudge when the same failure class repeats. Rides the existing
    repeated-failure signal (already bounded), spends one judge call for a concrete
    next step, and NEVER blocks. Fails open (returns "" on any error)."""
    try:
        from spec_io import load_spec, resolve_session_id
        from spec_judge import judge_hint

        task_id = resolve_session_id(input_data, default=None)
        spec = (load_spec(cwd, task_id) if task_id else None) or {}
        recent = " | ".join(
            (ledger.get("ran_commands") or [])[-6:] + [f"failure:{f}" for f in (ledger.get("failures") or [])[-4:]]
        )
        signal = (
            f"The same class of failure has repeated {count} times this session. "
            "The agent may be retrying the same approach instead of changing course."
        )
        return judge_hint(spec, signal=signal, recent=recent)
    except Exception:
        return ""


def _breaker_status_context(input_data: dict) -> str:
    """Minimal standing groundedness-breaker status (gap 6).

    Empty unless the breaker is armed or provisionally lifted, so the line only
    appears while the breaker is actually constraining the session. Fails open."""
    try:
        from breaker_state import load_breaker

        breaker = load_breaker(input_data)
        if breaker.get("breaker_armed"):
            claim = " ".join(str(breaker.get("breaker_claim") or "").split())[:60]
            return f"breaker: ARMED on '{claim}'" if claim else "breaker: ARMED"
        if breaker.get("breaker_provisional"):
            scope = " ".join(str(breaker.get("breaker_lift_scope") or "").split())[:60]
            return f"breaker: PROVISIONAL lift ({scope})" if scope else "breaker: PROVISIONAL lift"
    except Exception:
        return ""
    return ""


def _emit_context(
    input_data: dict,
    parts: list[str],
    *,
    guidance_map=None,
    failure_sig: str = "",
) -> None:
    filtered = parts
    cache_updates: dict[str, str] = {}
    try:
        filtered, cache_updates = prepare_posttool_parts(
            input_data,
            parts,
            failure_sig=failure_sig,
        )
    except Exception:
        pass
    body = "\n".join(p for p in filtered if p and p.strip())
    emit_posttool_context(
        input_data,
        body,
        guidance_map=guidance_map,
        cache_updates=cache_updates,
    )


def _plan_discover_job(input_data: dict, ledger: dict, spec: dict, tool_name: str):
    """Frontier-discovery gating + counter bookkeeping for the HEAVY workflow.

    Bumps the research-tool counter (every qualifying research tool counts toward the
    threshold) and returns (want_discover, discover_recorder). discover_recorder is a
    zero-arg callable that records one frontier discovery, or None. Fail-open: returns
    (False, None) on any error."""
    try:
        import db
        from evidence_policy import resolve_grade
        from heavy_workflow import frontier_tasks
        from ledger import ledger_key

        grade = resolve_grade(ledger, os.environ.get("UNIFABLE_GRADE"))
        research_tools = {"Read", "Grep", "Glob", "WebSearch", "WebFetch"}
        if grade != "HEAVY" or tool_name not in research_tools or len(frontier_tasks(spec)) >= 2:
            return False, None

        skey = ledger_key(input_data)
        n_tools, discoveries = db.frontier_bump_research(skey)
        if n_tools < 3 or discoveries >= 3:
            return False, None

        def discover_recorder():
            db.frontier_bump_discovery(skey)

        return True, discover_recorder
    except Exception:
        return False, None


def _spec_judge_thunks(
    input_data: dict,
    ledger: dict,
    cwd: str,
    tool_name: str,
    command: str,
    *,
    reads,
    fetched,
    ran,
    mcp_ev,
    research_ev,
    cmd_out,
    verification,
):
    """Plan the session-level spec judges (reconcile + discover) on a snapshot.

    Returns (task_id, activity, hygiene_changed, reconcile_thunk, discover_thunk,
    discover_recorder). The thunks are zero-arg network-only compute calls, or None
    when the job does not apply or a sibling already claimed this turn's spec judging
    (coalesce). hygiene_changed flags that deterministic hygiene mutated the snapshot
    so the caller persists it under the merge lock (this function never saves).
    Fail-open: returns all-None on any error."""
    none = (None, None, False, None, None, None)
    try:
        from citations import activity_from_ledger
        from posttool_judges import claim_spec_judging
        from spec_hygiene import apply_spec_hygiene
        from spec_io import load_spec, resolve_session_id
        from spec_judge import compute_frontier_additions, compute_reconcile_actions

        task_id = resolve_session_id(input_data, default=None)
        if not task_id:
            return none
        spec = load_spec(cwd, task_id)
        if not spec:
            return none

        activity = activity_from_ledger(ledger)
        evidence_changed = bool(reads or fetched or ran or mcp_ev or research_ev or cmd_out or verification)
        # Run hygiene on the in-memory snapshot only (for evidence_changed + a clean
        # compute snapshot); persistence happens under the merge lock, never here.
        hygiene_changed = bool(apply_spec_hygiene(spec, activity, cwd, added_sink={})[0])
        if hygiene_changed:
            evidence_changed = True

        want_reconcile = bool(evidence_changed and not (tool_name == "Bash" and is_spec_cli_command(command)))
        want_discover, discover_recorder = _plan_discover_job(input_data, ledger, spec, tool_name)

        if not (want_reconcile or want_discover):
            return (task_id, activity, hygiene_changed, None, None, None)

        # Coalesce session-level judging across the parallel tool batch: only the
        # first sibling within the window runs reconcile+discover.
        if not claim_spec_judging(input_data):
            return (task_id, activity, hygiene_changed, None, None, None)

        transcript_path = str(input_data.get("transcript_path") or "") or None
        reconcile_thunk = None
        discover_thunk = None
        if want_reconcile:

            def reconcile_thunk(s=spec, a=activity, t=transcript_path):
                return compute_reconcile_actions(s, a, transcript_path=t)

        if want_discover:

            def discover_thunk(s=spec, a=activity):
                return compute_frontier_additions(s, a)

        return (task_id, activity, hygiene_changed, reconcile_thunk, discover_thunk, discover_recorder)
    except Exception:
        return none


def _apply_spec_deltas(cwd, task_id, activity, hygiene_changed, reconcile_actions, frontier_additions, discover_recorder) -> str:
    """Persist hygiene + reconcile + frontier deltas to one base spec under
    update_spec's lock (hygiene first, then reconcile, then frontier so task ids stay
    collision-free), build the 'Spec update' context, and record a frontier discovery.

    Running hygiene inside the lock (instead of an earlier unlocked save) means a
    parallel sibling's citation/HEAVY-adoption updates merge onto the reloaded base
    instead of racing a whole-doc write. Fail-open: returns ""."""
    if not (hygiene_changed or reconcile_actions or frontier_additions):
        return ""
    try:
        from heavy_workflow import format_approach_board
        from spec_hygiene import apply_spec_hygiene
        from spec_io import update_spec
        from spec_judge import apply_frontier_additions, apply_reconcile_actions, build_spec_update_context

        captured: dict[str, list] = {"headlines": [], "added": []}

        def merge(base):
            if hygiene_changed:
                apply_spec_hygiene(base, activity, cwd, added_sink={})  # headlines intentionally unsurfaced (parity)
            if reconcile_actions:
                captured["headlines"] = apply_reconcile_actions(base, reconcile_actions, evidence=activity)
            if frontier_additions:
                captured["added"] = apply_frontier_additions(base, frontier_additions)

        merged = update_spec(cwd, task_id, merge)
        if merged is None:
            return ""
        headlines = captured["headlines"]
        added = captured["added"]
        discovery_context = build_spec_update_context(headlines) if headlines else ""
        if added:
            ids = ", ".join(t["id"] for t in added)
            frontier_context = (
                "Spec update:\n"
                f"Judge added frontier approach(s): {ids}. Explore ALL frontiers"
                " thoroughly (check each one). The judge compares evidence on Stop"
                " and may adopt the best over primary.\n" + format_approach_board(merged)
            )
            discovery_context = discovery_context + "\n" + frontier_context if discovery_context else frontier_context
            if discover_recorder is not None:
                discover_recorder()
        return discovery_context
    except Exception:
        return ""


def _run_judge_fanout(
    input_data: dict,
    ledger: dict,
    cwd: str,
    tool_name: str,
    command: str,
    executed_ok: bool,
    *,
    reads,
    fetched,
    ran,
    mcp_ev,
    research_ev,
    cmd_out,
    verification,
    repeat_count: int,
) -> tuple[str, str, str]:
    """Run the applicable PostToolUse judges concurrently and assemble their context.

    reconcile / discover / breaker-release / repeated-failure hint each cost a
    gpt-realtime-2 round-trip. They all route through the warm per-session daemon,
    so we fan the independent ones out concurrently under one wall-clock budget
    instead of stacking four ~90s deadlines straight-line under the host timeout.
    reconcile+discover are session-level (coalesced across a parallel tool batch);
    disarm+hint are tool/failure-specific (per-process). Returns
    (discovery_context, breaker_context, hint_text); fail-open on every path."""
    task_id, activity, hygiene_changed, reconcile_thunk, discover_thunk, discover_recorder = _spec_judge_thunks(
        input_data,
        ledger,
        cwd,
        tool_name,
        command,
        reads=reads,
        fetched=fetched,
        ran=ran,
        mcp_ev=mcp_ev,
        research_ev=research_ev,
        cmd_out=cmd_out,
        verification=verification,
    )

    # Breaker release (per-process; self-gates to "" when not armed / not a release tool).
    disarm_thunk = None
    try:
        from breaker_runtime import is_release_tool

        if executed_ok and is_release_tool(tool_name, input_data):

            def disarm_thunk():
                return _breaker_release_context(input_data, tool_name, executed_ok)
    except Exception:
        disarm_thunk = None

    # Repeated-failure hint (per-process; only when the same failure class repeats).
    hint_thunk = None
    if repeat_count:

        def hint_thunk():
            return _repeated_failure_hint(input_data, ledger, cwd, repeat_count)

    has_judges = bool(reconcile_thunk or discover_thunk or disarm_thunk or hint_thunk)
    reconcile_actions: list = []
    frontier_additions: list = []
    breaker_context = ""
    hint_text = ""
    if has_judges:
        try:
            from posttool_judges import POSTTOOL_JUDGE_BUDGET, run_posttool_judges

            result = run_posttool_judges(
                reconcile=reconcile_thunk,
                discover=discover_thunk,
                disarm=disarm_thunk,
                hint=hint_thunk,
                budget=POSTTOOL_JUDGE_BUDGET,
            )
            reconcile_actions = result.reconcile_actions
            frontier_additions = result.frontier_additions
            breaker_context = result.disarm_message or ""
            hint_text = result.hint_text or ""
        except Exception:
            # Orchestrator unavailable: skip the advisory judges entirely. We do NOT
            # fall back to sequential calls -- four straight-line ~90s judges would
            # blow the host PostToolUse timeout. Empty context is the bounded fail-open.
            reconcile_actions = []
            frontier_additions = []
            breaker_context = ""
            hint_text = ""

    # Persist hygiene + any spec deltas under the merge lock (hygiene runs per-process
    # even when the spec judges were coalesced or skipped).
    discovery_context = ""
    if task_id:
        discovery_context = _apply_spec_deltas(
            cwd, task_id, activity, hygiene_changed, reconcile_actions, frontier_additions, discover_recorder
        )
    return discovery_context, breaker_context, hint_text


def main() -> int:
    input_data = read_stdin_json()

    try:
        from judge_transport import bind_session

        bind_session(input_data)
    except Exception:
        pass

    cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))
    kinds = changed_kinds(input_data)
    failure = detect_failure(input_data)
    verification = verification_record(input_data)
    command = command_from_input(input_data)
    executed_ok = failure is None
    reads = [_abs(p, cwd) for p in read_targets(input_data)] if executed_ok else []
    fetched = fetched_url_targets(input_data) if executed_ok else []
    ran = ran_command(input_data) if executed_ok else None
    mcp_ev = mcp_evidence(input_data) if executed_ok else None
    research_ev = research_bash_evidence(input_data) if executed_ok else None
    cmd_out = command_output_evidence(input_data) if executed_ok else None
    tool_name = str(input_data.get("tool_name") or "unknown")

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
        if mcp_ev:
            add_unique(ledger, "tool_evidence", [mcp_ev])
        if research_ev:
            add_unique(ledger, "tool_evidence", [research_ev])
        if mcp_ev or research_ev:
            ledger["tool_evidence"] = ledger["tool_evidence"][-60:]
        if cmd_out:
            add_unique(ledger, "command_outputs", [cmd_out])
            ledger["command_outputs"] = ledger["command_outputs"][-60:]

    ledger = update_ledger(input_data, apply)

    # Concurrent judge fan-out: reconcile / discover / breaker-release / repeated-
    # failure hint each cost a gpt-realtime-2 round-trip. Run straight-line they
    # serialize ~90s deadlines under the host PostToolUse timeout; here they fan out
    # over the warm daemon under one wall-clock budget. reconcile+discover are
    # session-level and coalesced across a parallel tool batch; disarm+hint are
    # tool/failure-specific (per-process). Fail-open throughout.
    repeat = repeated_failure(ledger.get("failures", [])) if failure else None
    repeat_count = repeat[1] if repeat else 0
    discovery_context, breaker_context, hint_text = _run_judge_fanout(
        input_data,
        ledger,
        cwd,
        tool_name,
        command,
        executed_ok,
        reads=reads,
        fetched=fetched,
        ran=ran,
        mcp_ev=mcp_ev,
        research_ev=research_ev,
        cmd_out=cmd_out,
        verification=verification,
        repeat_count=repeat_count,
    )

    spec_context, guidance_map = _spec_context(input_data, tool_name, cwd)
    breaker_status_context = _breaker_status_context(input_data)

    if repeat:
        _sig, _count = repeat
        parts: list[str] = [
            "Tool failure observed. Do not report completion until "
            "it is fixed, isolated as a known baseline, or explicitly documented.",
        ]
        if hint_text:
            parts.append("Hint: " + hint_text)
        if spec_context:
            parts.append(spec_context)
        if breaker_status_context:
            parts.append(breaker_status_context)
        _emit_context(input_data, parts, guidance_map=guidance_map, failure_sig=_sig)
    else:
        parts: list[str] = []
        if failure and not spec_context:
            parts.append(
                "Tool failure observed. Do not report completion until "
                "it is fixed, isolated as a known baseline, or explicitly documented."
            )
        if spec_context:
            parts.append(spec_context)
        if discovery_context:
            parts.append(discovery_context)
        if breaker_status_context:
            parts.append(breaker_status_context)
        if breaker_context:
            parts.append(breaker_context)
        _emit_context(input_data, parts, guidance_map=guidance_map)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        emit_json({"systemMessage": f"Gate post-tool hook failed open: {exc}"})
        raise SystemExit(0)
