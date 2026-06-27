#!/usr/bin/env python3
"""Stop-path validation: run checks, adjudicate, heal, and finalize (unifable).

The completion gate's machinery -- auto_validate_spec runs each pending task's
runnable check (in parallel, under a wall-clock budget), feeds results to the judge,
applies adjustments, and self-heals judge-owned requirements. Host-agnostic;
re-exported by the spec.py facade.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from heavy_workflow import (
        adopted_frontier,
        advance_primary_if_ready,
        all_frontiers_rejected,
        all_frontiers_terminal,
        any_frontier_accepted,
        finalize_heavy_adoption,
        sync_heavy_phase,
    )
    from model_notify import notify_spec_update
    from spec_judge import (
        _JUDGE_HEAL_REASON_BRITTLE,
        _apply_adjustments,
        _judge_context,
        _judge_owned_open_tasks,
        judge_heal_own_requirements,
        judge_task,
        judge_tasks,
    )
    from spec_tasks import (
        JUDGE_MAX_UNRESOLVED_ADDED,
        RESOLVED_STATUSES,
        _apply_supersedes_bundle,
        _filter_judge_new_requirements,
        _is_heavy_spec,
        _new_task,
        _norm_title_conflicts,
        _normalize_title,
        _task_is_pending,
        all_tasks_validated,
        is_brittle_version_pinned_requirement,
    )
except ImportError:  # pragma: no cover
    from scripts.gate.heavy_workflow import (
        adopted_frontier,
        advance_primary_if_ready,
        all_frontiers_rejected,
        all_frontiers_terminal,
        any_frontier_accepted,
        finalize_heavy_adoption,
        sync_heavy_phase,
    )
    from scripts.gate.model_notify import notify_spec_update
    from scripts.gate.spec_judge import (
        _JUDGE_HEAL_REASON_BRITTLE,
        _apply_adjustments,
        _judge_context,
        _judge_owned_open_tasks,
        judge_heal_own_requirements,
        judge_task,
        judge_tasks,
    )
    from scripts.gate.spec_tasks import (
        JUDGE_MAX_UNRESOLVED_ADDED,
        RESOLVED_STATUSES,
        _apply_supersedes_bundle,
        _filter_judge_new_requirements,
        _is_heavy_spec,
        _new_task,
        _norm_title_conflicts,
        _normalize_title,
        _task_is_pending,
        all_tasks_validated,
        is_brittle_version_pinned_requirement,
    )


_OUTPUT_LIMIT = 4000  # cap on captured check output stored in the spec


_CHECK_TIMEOUT = 600


def _should_replay_failed_check(task: dict[str, Any]) -> bool:
    """True when a failed task should replay stored exit/output instead of re-running."""
    if str(task.get("status") or "") != "failed":
        return False
    return task.get("replay_failed") is True


def _check_inputs_for_task(
    task: dict[str, Any],
    cwd: str | Path,
    deadline: float | None,
) -> tuple[int, str]:
    """Return (exit_code, output) for judge validation.

    Open tasks get a fresh check run (bounded by the Stop wall-clock budget).
    Failed tasks replay stored output only when ``replay_failed`` is true on
    the task (escape hatch for expensive checks).
    """
    if _should_replay_failed_check(task):
        exit_code = task.get("exit")
        return (
            int(exit_code if exit_code is not None else 1),
            str(task.get("output") or ""),
        )
    if deadline is not None:
        ct = max(1, int(min(_CHECK_TIMEOUT, deadline - time.monotonic())))
        return run_check(task.get("check", ""), cwd=cwd, timeout=ct)
    return run_check(task.get("check", ""), cwd=cwd)


def _check_parallelism() -> int:
    """Max concurrent stop-path check subprocesses (UNIFABLE_CHECK_PARALLELISM)."""
    try:
        n = int(os.environ.get("UNIFABLE_CHECK_PARALLELISM", "8") or "8")
    except (TypeError, ValueError):
        n = 8
    return max(1, min(n, 64))


def _prior_exit_for_failed_task(task: dict[str, Any]) -> int | None:
    if str(task.get("status") or "") != "failed" or _should_replay_failed_check(task):
        return None
    raw_exit = task.get("exit")
    if raw_exit is None:
        return None
    try:
        return int(raw_exit)
    except (TypeError, ValueError):
        return 1


def _apply_runnable_check_result(it: dict[str, Any], exit_code: int, output: str) -> None:
    """Fill a pending validate item from a subprocess result."""
    it.pop("_check_pending", None)
    if exit_code == 127 and "not found" in (output or "").lower():
        it["exit_code"] = None
        it["output"] = ""
        it["evidence_only"] = True
        it["prior_exit"] = None
        return
    it["exit_code"] = exit_code
    it["output"] = output


def _collect_stop_validate_item(task: dict[str, Any]) -> dict[str, Any]:
    """Build one stop-validation item; runnable checks may stay pending for parallel run."""
    check = str(task.get("check") or "")
    if not is_runnable_check(check):
        return {
            "task": task,
            "kind": "validate",
            "exit_code": None,
            "output": "",
            "evidence_only": True,
            "prior_exit": None,
        }
    prior_exit = _prior_exit_for_failed_task(task)
    if _should_replay_failed_check(task):
        exit_code = task.get("exit")
        return {
            "task": task,
            "kind": "validate",
            "exit_code": int(exit_code if exit_code is not None else 1),
            "output": str(task.get("output") or ""),
            "prior_exit": prior_exit,
        }
    return {
        "task": task,
        "kind": "validate",
        "_check_pending": True,
        "prior_exit": prior_exit,
    }


def _run_stop_checks_parallel(items: list[dict[str, Any]], cwd: str | Path, deadline: float | None) -> list[dict[str, Any]]:
    """Run pending shell checks concurrently; drop items the budget no longer covers."""
    pending = [it for it in items if it.get("_check_pending")]
    if not pending:
        return items
    if deadline is not None and time.monotonic() >= deadline:
        return [it for it in items if not it.pop("_check_pending", None)]
    workers = min(len(pending), _check_parallelism())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_check_inputs_for_task, it["task"], cwd, deadline): it for it in pending}
        for fut in as_completed(futures):
            it = futures[fut]
            try:
                exit_code, output = fut.result()
            except Exception as exc:  # noqa: BLE001
                exit_code, output = 127, f"(check failed to run: {exc})"
            _apply_runnable_check_result(it, exit_code, output)
    return [it for it in items if not it.get("_check_pending")]


def _apply_check_result(
    spec: dict[str, Any],
    task: dict[str, Any],
    exit_code: int,
    output: str,
    verdict: int,
    reason: str,
    new_reqs: list[dict[str, str]],
    *,
    frontier_outcome: str = "",
    prior_exit: int | None = None,
) -> list[str]:
    """Record a check+judge outcome on the task and notify. Mutates spec in place."""
    tid = str(task.get("id") or "")
    prefix: list[str] = []
    if prior_exit is not None and prior_exit != exit_code:
        prefix.append(f"{tid} check re-run: exit {exit_code} (was {prior_exit}).")
    task["exit"] = exit_code
    task["output"] = output
    task["judge_verdict"] = verdict
    task["judge_reason"] = reason
    kind = str(task.get("approach_kind") or "requirement")
    task["attempts"] = int(task.get("attempts") or 0) + 1
    if task.get("status") == "retracted" and task.get("added_by") == "judge":
        headline = f"{tid} retracted by judge: {str(task.get('judge_reason') or reason)}"
        notify_spec_update(
            spec,
            headline,
            highlight_task=tid,
        )
        return [headline]
    added: list[str] = []
    extra_headlines: list[str] = []
    # Apply new requirements + supersedes BEFORE mutating the current task status so
    # a batch Stop can supersede sibling tasks without a later item re-failing them.
    existing_pairs = {
        (str(t.get("title") or "").strip(), str(t.get("check") or "").strip())
        for t in (spec.get("tasks") or [])
        if isinstance(t, dict)
    }
    existing_norm_titles = {_normalize_title(t.get("title")) for t in (spec.get("tasks") or []) if isinstance(t, dict)}
    judge_unresolved = sum(
        1
        for t in (spec.get("tasks") or [])
        if isinstance(t, dict) and t.get("added_by") == "judge" and t.get("status") not in RESOLVED_STATUSES
    )
    filtered_reqs = _filter_judge_new_requirements(new_reqs, existing_pairs, existing_norm_titles)
    for req in filtered_reqs:
        if judge_unresolved >= JUDGE_MAX_UNRESOLVED_ADDED:
            break
        pair = (str(req.get("title") or "").strip(), str(req.get("check") or "").strip())
        if pair in existing_pairs:
            continue
        norm_title = _normalize_title(req.get("title"))
        if norm_title and (norm_title in existing_norm_titles or _norm_title_conflicts(norm_title, existing_norm_titles)):
            continue
        spec.setdefault("tasks", [])
        nt = _new_task(spec, req["title"], req["check"])
        nt["added_by"] = "judge"
        spec["tasks"].append(nt)
        existing_pairs.add(pair)
        existing_norm_titles.add(norm_title)
        judge_unresolved += 1
        new_tid = nt["id"]
        added.append(new_tid)
        supersedes = req.get("supersedes") or []
        if isinstance(supersedes, list) and supersedes:
            extra_headlines.extend(
                _apply_supersedes_bundle(
                    spec,
                    new_tid,
                    [str(x) for x in supersedes],
                    reason=reason,
                )
            )
    if str(task.get("status") or "") in ("superseded", "retracted"):
        return extra_headlines
    if kind == "frontier":
        if frontier_outcome == "rejected_approach":
            task["status"] = "rejected_approach"
        elif frontier_outcome == "accepted_approach":
            task["status"] = "accepted_approach"
        else:
            task["status"] = "failed"
    elif kind == "primary" and adopted_frontier(spec) is not None:
        winner = adopted_frontier(spec)
        wid = str(winner.get("id") or "") if winner else ""
        task["status"] = "superseded"
        task["judge_reason"] = f"Superseded by adopted frontier {wid}."
    else:
        task["status"] = "validated" if verdict == 1 else "failed"
    # Validated-with-evidence: when a task validates off a runnable check, record
    # HOW it was proven (the command + its deterministic exit) so the board and the
    # main model see the grounding, not just a status flip. The judge still decides
    # (verdict==1); the check + exit are the evidence behind that decision.
    if task["status"] == "validated" and is_runnable_check(str(task.get("check") or "")):
        task["validated_by"] = f"`{str(task.get('check') or '').strip()}` (exit {exit_code})"
    advance_primary_if_ready(spec)
    sync_heavy_phase(spec)
    if kind == "frontier" and task["status"] == "accepted_approach":
        headline = f"{tid} frontier accepted by judge (check passed): {reason}."
    elif kind == "frontier" and task["status"] == "rejected_approach":
        headline = f"{tid} frontier ruled out by judge: {reason}."
        if all_frontiers_rejected(spec):
            headline += " All frontiers rejected — primary phase unlocked."
    elif verdict == 1:
        if task.get("validated_by"):
            headline = f"{tid} validated by {task['validated_by']}; judge accepted the evidence."
        else:
            headline = f"{tid} check passed (exit {exit_code}); judge accepted the evidence."
        if added:
            headline += f" Judge added {', '.join(added)}."
        if all_tasks_validated(spec)[0]:
            headline += " Completion breaker open."
    else:
        headline = f"{tid} check ran (exit {exit_code}); judge rejected the evidence."
        if added:
            headline += f" Judge added {', '.join(added)}."
    notify_spec_update(
        spec,
        headline,
        highlight_task=tid,
    )
    return prefix + [headline] + extra_headlines


def _validate_one_task(
    spec: dict[str, Any],
    task: dict[str, Any],
    cwd: str | Path,
    *,
    transcript_path: str | None = None,
) -> list[str]:
    """Validate ONE task (check+judge). Mutates spec in place."""
    exit_code, output = _check_inputs_for_task(task, cwd, deadline=None)
    transcript, plan_mode = _judge_context(transcript_path)
    if transcript:
        verdict, reason, new_reqs, frontier_outcome = judge_task(
            spec,
            task,
            exit_code,
            output,
            transcript=transcript,
            plan_mode=plan_mode,
        )
    else:
        verdict, reason, new_reqs, frontier_outcome = judge_task(
            spec,
            task,
            exit_code,
            output,
            plan_mode=plan_mode,
        )
    return _apply_check_result(
        spec,
        task,
        exit_code,
        output,
        verdict,
        reason,
        new_reqs,
        frontier_outcome=frontier_outcome,
    )


def auto_validate_spec(
    spec: dict[str, Any],
    cwd: str | Path,
    *,
    time_budget: float | None = None,
    transcript_path: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Validate every open task on stop. Mutates spec in place.

    Open tasks (including failed) get fresh checks bounded by the remaining
    wall-clock budget unless ``replay_failed`` is set on the task. Runnable
    checks run concurrently (``UNIFABLE_CHECK_PARALLELISM``, default 8). One
    ask_structured round-trip judges all tasks together
    from shared context (goal, board, transcript, check outputs). When
    time_budget is set, check runs stop at the deadline; remaining tasks stay
    open and are picked up on the next stop."""
    messages: list[str] = []
    messages.extend(heal_judge_owned_requirements(spec, transcript_path=transcript_path))
    deadline = (time.monotonic() + time_budget) if time_budget is not None else None

    pending: list[tuple[int, dict[str, Any]]] = []
    advance_primary_if_ready(spec)
    for idx, task in enumerate(list(spec.get("tasks") or [])):
        if not isinstance(task, dict) or not _task_is_pending(task):
            continue
        pending.append((idx, task))

    if _is_heavy_spec(spec):
        pending.sort(
            key=lambda it: (
                0
                if str(it[1].get("approach_kind") or "") == "frontier"
                else 1
                if str(it[1].get("approach_kind") or "") == "primary"
                else 2,
                int(it[1].get("attempts") or 0),
                it[0],
            )
        )
    else:
        pending.sort(key=lambda it: (int(it[1].get("attempts") or 0), it[0]))
    pending_tasks = [task for _, task in pending]

    transcript, plan_mode = _judge_context(transcript_path)

    slots: list[dict[str, Any]] = []
    for task in pending_tasks:
        if deadline is not None and time.monotonic() >= deadline:
            break
        slots.append(_collect_stop_validate_item(task))
    items = _run_stop_checks_parallel(slots, cwd, deadline)

    if items:
        spec.pop("_stop_adjust_headlines", None)
        verdicts = judge_tasks(spec, items, transcript=transcript, plan_mode=plan_mode, evidence=evidence)
        for h in spec.pop("_stop_adjust_headlines", []):
            if h not in messages:
                messages.append(h)
        for it, (verdict, reason, new_reqs, frontier_outcome) in zip(items, verdicts):
            task = it["task"]
            revised = task.pop("_revise_this_stop", None)
            exit_code, output = it["exit_code"], it["output"]
            if task.pop("_check_stale", None):
                exit_code, output = _check_inputs_for_task(task, cwd, deadline)
            if revised and verdict != 1:
                task["status"] = "pending"
                continue
            messages.extend(
                _apply_check_result(
                    spec,
                    task,
                    exit_code,
                    output,
                    verdict,
                    reason,
                    new_reqs,
                    frontier_outcome=frontier_outcome,
                    prior_exit=it.get("prior_exit"),
                )
            )

    # HEAVY adoption: deterministic finalization once frontiers are terminal.
    if _is_heavy_spec(spec):
        if all_frontiers_terminal(spec) and any_frontier_accepted(spec):
            adopt_headlines = finalize_heavy_adoption(spec)
            if adopt_headlines:
                messages.extend(adopt_headlines)
                notify_spec_update(spec, adopt_headlines[0])

    return spec, messages


_CHECK_BUILTINS = frozenset(
    {
        "test",
        "[",
        "[[",
        "cd",
        ":",
        "true",
        "false",
        "echo",
        "printf",
        "cat",
        "ls",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "sed",
        "awk",
        "find",
        "head",
        "tail",
        "wc",
        "diff",
        "cmp",
        "sort",
        "uniq",
        "tr",
        "cut",
        "jq",
        "yq",
        "xargs",
        "stat",
        "file",
        "touch",
        "cp",
        "mv",
        "rm",
        "mkdir",
        "python",
        "python3",
        "python2",
        "pip",
        "pip3",
        "pytest",
        "git",
        "bash",
        "sh",
        "zsh",
        "node",
        "npm",
        "npx",
        "pnpm",
        "yarn",
        "bun",
        "deno",
        "go",
        "cargo",
        "rustc",
        "make",
        "just",
        "ruff",
        "mypy",
        "pyright",
        "tsc",
        "eslint",
        "curl",
        "wget",
        "ln",
        "tee",
        "env",
    }
)


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


_STRUCTURE_OPS = ("&&", "||", "|", ";", ">", "<", "$(", "`", "$((")


_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,6}$")


def _has_shell_structure(text: str, tail_tokens: list[str]) -> bool:
    """Command-line structure that a natural-language sentence does not have:
    a shell operator anywhere, or a flag / path / file-extension token."""
    if any(op in text for op in _STRUCTURE_OPS):
        return True
    for t in tail_tokens:
        if t.startswith("-") or "/" in t or _FILE_EXT_RE.search(t):
            return True
    return False


def is_runnable_check(check: str) -> bool:
    """True when *check* is an executable shell command, False when it is prose / a
    natural-language evidence description (e.g. "Slack search returned a relevant
    direct message", "Pull request metadata shows open draft state"). Non-runnable
    checks are routed to evidence_only judging instead of being shell-executed
    (the exit-127 loop).

    A long sentence whose first word merely happens to BE a real command (`pr`,
    "PR"/"Slack") is still prose: it carries no command-line structure."""
    s = (check or "").strip()
    if not s:
        return False
    try:
        toks = shlex.split(s)
    except ValueError:
        toks = s.split()
    # Skip leading `VAR=value` env assignments to find the real command word.
    idx = 0
    while idx < len(toks) and _ENV_ASSIGN_RE.match(toks[idx]):
        idx += 1
    cmd_toks = toks[idx:]
    if not cmd_toks:
        return False
    first = cmd_toks[0]
    base = os.path.basename(first)
    first_is_command = (
        base in _CHECK_BUILTINS
        or first in _CHECK_BUILTINS
        or first.startswith(("./", "/", "~", "$", "("))
        or shutil.which(first) is not None
    )
    if not first_is_command:
        return False
    if first.startswith(("./", "/", "~", "$", "(")):
        return True
    if len(cmd_toks) == 1:
        return True
    if _has_shell_structure(s, cmd_toks[1:]):
        return True
    # Flagless multi-word command with no structure: runnable only when short
    # (`git status`, `npm test`, `make check`); a longer wordy string is prose.
    return len(cmd_toks) <= 3


def run_check(check: str, cwd: str | Path = ".", timeout: int = _CHECK_TIMEOUT) -> tuple[int, str]:
    """Run a task's check command -> (exit_code, combined stdout+stderr, capped)."""
    try:
        proc = subprocess.run(
            check,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out[-_OUTPUT_LIMIT:]
    except subprocess.TimeoutExpired:
        return 124, f"(check timed out after {timeout}s)"
    except Exception as exc:  # noqa: BLE001
        return 127, f"(check failed to run: {exc})"


def deterministic_heal_judge_requirements(spec: dict[str, Any]) -> list[str]:
    """Harness-owned fixes for judge tasks the agent cannot resolve."""
    adjustments: list[dict[str, str]] = []
    for t in _judge_owned_open_tasks(spec):
        tid = str(t.get("id") or "")
        if not tid:
            continue
        title = str(t.get("title") or "")
        check = str(t.get("check") or "")
        if is_brittle_version_pinned_requirement(title, check):
            adjustments.append(
                {
                    "id": tid,
                    "action": "retract",
                    "reason": _JUDGE_HEAL_REASON_BRITTLE,
                }
            )
    if not adjustments:
        return []
    return _apply_adjustments(spec, {"adjust_requirements": adjustments})


def heal_judge_owned_requirements(
    spec: dict[str, Any],
    *,
    transcript_path: str | None = None,
) -> list[str]:
    """Self-heal judge-owned requirements before Stop validation."""
    headlines = deterministic_heal_judge_requirements(spec)
    try:
        from heavy_workflow import advance_primary_if_ready, sync_heavy_phase

        if sync_heavy_phase(spec):
            pass
        advance_primary_if_ready(spec)
    except Exception:
        pass
    if _judge_owned_open_tasks(spec):
        headlines.extend(judge_heal_own_requirements(spec, transcript_path=transcript_path))
        try:
            from heavy_workflow import advance_primary_if_ready, sync_heavy_phase

            sync_heavy_phase(spec)
            advance_primary_if_ready(spec)
        except Exception:
            pass
    return headlines
