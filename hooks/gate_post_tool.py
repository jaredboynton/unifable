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
from collections.abc import Callable
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
    """Deprecated: PostToolUse no longer narrates standing breaker state to the
    model. The PreToolUse one-shot lift/block notify is the single source of
    breaker guidance (it arrives at a moment the model can act on it; PostToolUse
    does not gate). Kept as a stub returning "" so existing callers/importers do
    not break. Fails open."""
    _ = input_data
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


def _spec_judge_plan(
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
    """Decide whether the session-level spec judges (reconcile + discover) apply.

    Returns (task_id, activity, hygiene_changed, want_reconcile, want_discover,
    discover_recorder). want_* are booleans the caller uses to spawn the background
    job; the actual judge calls + spec mutation happen in the detached child, never
    here. hygiene_changed flags that deterministic hygiene mutated the snapshot so the
    caller persists it. A sibling that already claimed this turn's spec judging
    (coalesce) gets want_*=False. Fail-open: returns all-falsey on any error."""
    none = (None, None, False, False, False, None)
    try:
        from citations import activity_from_ledger
        from posttool_judges import claim_spec_judging
        from spec_hygiene import apply_spec_hygiene
        from spec_io import load_spec, resolve_session_id

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
            return (task_id, activity, hygiene_changed, False, False, None)

        # Coalesce session-level judging across the parallel tool batch: only the
        # first sibling within the window spawns the background reconcile+discover.
        if not claim_spec_judging(input_data):
            return (task_id, activity, hygiene_changed, False, False, None)

        return (task_id, activity, hygiene_changed, want_reconcile, want_discover, discover_recorder)
    except Exception:
        return none


def _apply_hygiene_only(cwd, task_id, activity) -> None:
    """Persist deterministic spec hygiene under update_spec's lock. The advisory
    reconcile/discover deltas are applied by the detached background job, not here.
    Fail-open: silent on any error."""
    try:
        from spec_hygiene import apply_spec_hygiene
        from spec_io import update_spec

        def merge(base):
            apply_spec_hygiene(base, activity, cwd, added_sink={})

        update_spec(cwd, task_id, merge)
    except Exception:
        pass


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
    """Spawn the advisory session judges in the background; run the fast ones inline.

    reconcile + frontier-discover are ADVISORY board maintenance that gate nothing,
    so they no longer block the hot path on a gpt-realtime-2 round-trip. We compute
    whether they apply (hygiene snapshot + coalesce claim), then -- if so -- fork a
    detached child (`posttool_background.spawn_reconcile_job`) that runs the judges,
    applies the deltas under the spec lock, and ENQUEUES the resulting context for the
    next PreToolUse to drain. Deterministic spec hygiene still persists inline.

    breaker-release (disarm) and the repeated-failure hint are load-bearing /
    tool-specific and stay synchronous, fanned out concurrently under one budget.
    Returns (discovery_context, breaker_context, hint_text); discovery_context is now
    always "" here (it arrives later via the PreToolUse drain). Fail-open throughout."""
    task_id, activity, hygiene_changed, want_reconcile, want_discover, _discover_recorder = _spec_judge_plan(
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

    # Fire-and-forget the advisory session judges (reconcile/discover). The child
    # records its own frontier discovery, so discover_recorder is only used to bump
    # the counter when discovery actually adds a frontier -- handled inside the job.
    if want_reconcile or want_discover:
        try:
            from posttool_background import spawn_reconcile_job

            spawn_reconcile_job(input_data, want_reconcile=want_reconcile, want_discover=want_discover)
        except Exception:
            pass

    # Persist deterministic hygiene inline under the merge lock (the background job
    # also runs hygiene on its reloaded base, so this is belt-and-suspenders and never
    # races the whole-doc write).
    if task_id and hygiene_changed:
        _apply_hygiene_only(cwd, task_id, activity)

    # Breaker release (disarm): the LIFT moves OFF the hot path. Arming stays
    # synchronous in PreToolUse; here we only dispatch a detached worker that runs
    # the release judge under breaker_lock and enqueues the disarm message for the
    # next PreToolUse (or Stop) to drain. PreToolUse's armed branch re-runs the
    # release judge on every armed call (breaker_orchestration.evaluate_pre_tool,
    # elif armed -> disarm_judge), so a slow/dead worker self-heals -- the worker
    # just makes the lift usually-already-done. Fail-open: a dispatch failure leaves
    # the arm, which PreToolUse/Stop converge (the safe direction).
    try:
        from breaker_runtime import is_release_tool

        if executed_ok and is_release_tool(tool_name, input_data):
            fresh = _fresh_tool_block(input_data, tool_name, executed_ok)
            if fresh:
                from breaker_state import load_breaker

                breaker = load_breaker(input_data)
                if breaker.get("breaker_armed") or breaker.get("breaker_provisional"):
                    from breaker_release_lane import spawn_release_job

                    spawn_release_job(input_data, tool_name, fresh)
    except Exception:
        pass

    # Repeated-failure hint (per-process; only when the same failure class repeats).
    hint_thunk: Callable[[], str] | None = None
    if repeat_count:

        def hint_thunk() -> str:
            return _repeated_failure_hint(input_data, ledger, cwd, repeat_count)

    breaker_context = ""
    hint_text = ""
    if hint_thunk is not None:
        try:
            from posttool_judges import POSTTOOL_JUDGE_BUDGET, run_posttool_judges

            result = run_posttool_judges(
                hint=hint_thunk,
                budget=POSTTOOL_JUDGE_BUDGET,
            )
            hint_text = result.hint_text or ""
        except Exception:
            hint_text = ""

    return "", breaker_context, hint_text


def _test_after_edit_context(input_data: dict) -> str:
    """Test-after-edit, folded in-process from the former second PostToolUse hook.
    Self-gates to "" unless UNIFABLE_TEST_AFTER_EDIT=1 and the tool is an edit tool
    with a discoverable runner; debounce + per-run timeout live inside. Fail-open."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_after_edit import compute_test_context

        return compute_test_context(input_data) or ""
    except Exception:
        return ""


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
    test_context = _test_after_edit_context(input_data)

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
        if test_context:
            parts.append(test_context)
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
        if breaker_context:
            parts.append(breaker_context)
        if test_context:
            parts.append(test_context)
        _emit_context(input_data, parts, guidance_map=guidance_map)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        emit_json({"systemMessage": f"Gate post-tool hook failed open: {exc}"})
        raise SystemExit(0)
