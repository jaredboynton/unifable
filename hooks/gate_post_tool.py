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
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

from ledger import add_unique, emit_json, load_ledger, read_stdin_json, update_ledger
from model_notify import (
    build_citation_sync_context,
    build_posttool_spec_context,
    is_spec_cli_command,
)
from parse_tool_result import (
    changed_kinds,
    command_from_input,
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
from posttool_notify import emit_posttool_context, prepare_posttool_parts, should_suppress_cite_only
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


def _citation_sync_headline(added: dict[str, list[str]]) -> str:
    """One batched headline naming the cites auto-synced this PostToolUse call (gap 1).

    Per-turn batch: a single line covering everything sync_citations_from_activity
    appended for this tool, capped so a wide read does not flood the channel."""
    prior = [str(u) for u in (added.get("prior_art") or []) if str(u).strip()]
    repo = [str(p) for p in (added.get("repo_context") or []) if str(p).strip()]
    total = len(prior) + len(repo)
    if not total:
        return ""
    segs: list[str] = []
    if prior:
        segs.append("prior_art<-fetch [" + ", ".join(prior[:3]) + ("..." if len(prior) > 3 else "") + "]")
    if repo:
        segs.append("repo_context<-read [" + ", ".join(repo[:3]) + ("..." if len(repo) > 3 else "") + "]")
    return f"synced {total} cite(s): " + "; ".join(segs)


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

    ledger = update_ledger(input_data, apply)

    discovery_context = ""
    citation_context = ""
    try:
        from citations import activity_from_ledger
        from evidence_policy import resolve_grade
        from heavy_workflow import format_approach_board, frontier_tasks
        from spec_hygiene import apply_spec_hygiene
        from spec_io import load_spec, resolve_session_id, save_spec
        from spec_judge import judge_discover_frontiers

        task_id = resolve_session_id(input_data, default=None)
        grade = resolve_grade(ledger, os.environ.get("UNIFABLE_GRADE"))
        research_tools = {"Read", "Grep", "Glob", "WebSearch", "WebFetch"}
        if task_id:
            spec = load_spec(cwd, task_id)
            activity = activity_from_ledger(ledger)
            citation_added: dict[str, list[str]] = {}
            if spec and apply_spec_hygiene(spec, activity, cwd, added_sink=citation_added)[0]:
                save_spec(cwd, task_id, spec)
                _cite_headline = _citation_sync_headline(citation_added)
                if _cite_headline:
                    try:
                        _led = load_ledger(input_data)
                        if should_suppress_cite_only(spec, _led, _cite_headline):
                            _cite_headline = ""
                    except Exception:
                        pass
                    if _cite_headline:
                        citation_context = build_citation_sync_context(_cite_headline)
            if spec and grade == "HEAVY" and tool_name in research_tools and len(frontier_tasks(spec)) < 2:
                if grade == "HEAVY":
                    n_tools = int(ledger.get("frontier_research_tools") or 0) + 1
                    discoveries = int(ledger.get("frontier_discovery_count") or 0)

                    def bump_discovery(ld):
                        ld["frontier_research_tools"] = n_tools

                    update_ledger(input_data, bump_discovery)

                    if n_tools >= 3 and discoveries < 3:
                        added = judge_discover_frontiers(spec, activity)
                        if added:
                            save_spec(cwd, task_id, spec)

                            def record_discovery(ld):
                                ld["frontier_discovery_count"] = discoveries + 1

                            update_ledger(input_data, record_discovery)
                            ids = ", ".join(t["id"] for t in added)
                            discovery_context = (
                                "Spec update:\n"
                                f"Judge added frontier approach(s): {ids}. Explore ALL frontiers"
                                " thoroughly (check each one). The judge compares evidence on Stop"
                                " and may adopt the best over primary.\n" + format_approach_board(spec)
                            )
    except Exception:
        pass

    spec_context, guidance_map = _spec_context(input_data, tool_name, cwd)
    breaker_context = _breaker_release_context(input_data, tool_name, executed_ok)
    breaker_status_context = _breaker_status_context(input_data)

    repeat = repeated_failure(ledger.get("failures", [])) if failure else None
    if repeat:
        _sig, count = repeat
        hint = _repeated_failure_hint(input_data, ledger, cwd, count)
        parts: list[str] = [
            "Tool failure observed. Do not report completion until "
            "it is fixed, isolated as a known baseline, or explicitly documented.",
        ]
        if hint:
            parts.append("Hint: " + hint)
        if citation_context:
            parts.append(citation_context)
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
        if citation_context:
            parts.append(citation_context)
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
