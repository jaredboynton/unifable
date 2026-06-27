#!/usr/bin/env python3
"""Command-line interface for the unifable spec artifact (unifable).

The `unifable` subcommands the model drives the spec with -- restate / add-task /
set-primary / add-frontier / contract / doctor -- plus argv dispatch.
Specs are CLI-only (never model-writable via Edit/Write). Re-exported by the
spec.py facade, which keeps the runnable __main__ entry point.
"""

from __future__ import annotations

import argparse
import os
import sys

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from heavy_workflow import frontier_tasks
    from model_notify import format_spec_status, notify_spec_update
    from spec_contracts import contract_string
    from spec_io import (
        _find_fragmented_specs,
        canonical_project_root,
        ensure_spec_scaffold,
        format_spec_location,
        load_spec,
        resolve_session_id,
        resolve_session_id_with_source,
        save_spec,
        spec_path,
    )
    from spec_schema import GRADES, spec_template
    from spec_tasks import _new_task, append_frontier_task, set_primary_task
except ImportError:  # pragma: no cover
    from scripts.gate.heavy_workflow import frontier_tasks
    from scripts.gate.model_notify import format_spec_status, notify_spec_update
    from scripts.gate.spec_contracts import contract_string
    from scripts.gate.spec_io import (
        _find_fragmented_specs,
        canonical_project_root,
        ensure_spec_scaffold,
        format_spec_location,
        load_spec,
        resolve_session_id,
        resolve_session_id_with_source,
        save_spec,
        spec_path,
    )
    from scripts.gate.spec_schema import GRADES, spec_template
    from scripts.gate.spec_tasks import _new_task, append_frontier_task, set_primary_task


# Copy-pasteable correct invocations, surfaced both in argparse errors (via
# _HintingParser) and when the positional/flag resolver finds a missing arg.
# The natural failure the model hits is `add-task '<title>'` with no --check;
# these turn the bare argparse dump into one-shot remediation.
_USAGE_HINTS = {
    "add-task": (
        "Try: unifable add-task --title '<requirement>' --check '<runnable check>' "
        "(title+check may also be passed as two positional args)."
    ),
    "set-primary": "Try: unifable set-primary --title '<approach>' --check '<runnable proof>'.",
    "add-frontier": "Try: unifable add-frontier --title '<approach>' --check '<exploration check>'.",
    "restate": "Try: unifable restate '<goal in your own words>' (positional goal, not --goal).",
}


class _HintingParser(argparse.ArgumentParser):
    """ArgumentParser that appends a copy-pasteable correct command to errors."""

    def error(self, message: str):  # noqa: D401 — argparse override
        self.print_usage(sys.stderr)
        sub = (self.prog or "").split()[-1]
        hint = _USAGE_HINTS.get(sub)
        out = f"{self.prog}: error: {message}"
        if hint:
            out += f"\n{hint}"
        self.exit(2, out + "\n")


def _resolve_title_check(args: argparse.Namespace, label: str) -> tuple[str | None, str | None]:
    """Resolve title/check from flags, falling back to positional args.

    Returns (None, None) and prints actionable guidance when either is missing,
    so the natural `add-task '<title>'` (no check) attempt gets a crisp fix
    instead of a generic argparse dump."""
    title = args.title
    check = args.check
    rest = list(getattr(args, "rest", []) or [])
    if title is None and rest:
        title = rest.pop(0)
    if check is None and rest:
        check = rest.pop(0)
    missing: list[str] = []
    if not title:
        missing.append("--title '<...>'")
    if not check:
        missing.append("--check '<runnable check>'")
    if missing:
        print(f"{label} needs {', '.join(missing)}. {_USAGE_HINTS[label]}", file=sys.stderr)
        return None, None
    return title, check


def _cmd_contract(args: argparse.Namespace) -> int:
    grade = (args.grade or "STANDARD").upper()
    if grade not in GRADES:
        print(f"Unknown grade '{grade}'; expected one of {', '.join(GRADES)}.", file=sys.stderr)
        return 1
    print(contract_string(grade, getattr(args, "require_evidence", False)))
    return 0


def _cmd_add_task(args: argparse.Namespace) -> int:
    title, check = _resolve_title_check(args, "add-task")
    if title is None:
        return 2
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        # Self-heal: creation is normally the hook's job, but if the spec is
        # missing, the agent's first add-task seeds a requires_tasks scaffold
        # (goal taken from the requirement) rather than dead-ending on `create`,
        # which the agent is not allowed to run.
        spec = spec_template()
        spec["restated_goal"] = title.strip()
        spec["acceptance_criteria"] = []
        spec["repo_context"] = []
        spec["prior_art"] = []
        spec["tasks"] = []
        spec["requires_tasks"] = True
    spec.setdefault("tasks", [])
    task = _new_task(spec, title, check)
    spec["tasks"].append(task)
    save_spec(args.root, args.task_id, spec)
    print(f"Added {task['id']}: {task['title']}")
    notify_spec_update(
        spec,
        f"Requirement {task['id']} added: {task['title']}.",
        highlight_task=task["id"],
        surface="stdout_only",
    )
    return 0


def _cmd_set_primary(args: argparse.Namespace) -> int:
    title, check = _resolve_title_check(args, "set-primary")
    if title is None:
        return 2
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        print(f"No spec at {spec_path(args.root, args.task_id)}.", file=sys.stderr)
        return 1
    try:
        task = set_primary_task(spec, title, check)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_spec(args.root, args.task_id, spec)
    print(f"Primary approach set: {task['id']} (blocked until frontiers ruled out).")
    notify_spec_update(
        spec,
        f"Primary approach {task['id']} set (blocked until frontiers rejected).",
        surface="stdout_only",
    )
    return 0


def _cmd_add_frontier(args: argparse.Namespace) -> int:
    title, check = _resolve_title_check(args, "add-frontier")
    if title is None:
        return 2
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        print(f"No spec at {spec_path(args.root, args.task_id)}.", file=sys.stderr)
        return 1
    task = append_frontier_task(spec, title, check, added_by="agent")
    save_spec(args.root, args.task_id, spec)
    n = len(frontier_tasks(spec))
    print(f"Frontier approach added: {task['id']} ({n} total).")
    notify_spec_update(
        spec,
        f"Frontier {task['id']} added ({n}/2 for declare phase).",
        surface="stdout_only",
    )
    return 0


def _cmd_restate(args: argparse.Namespace) -> int:
    """Set restated_goal in the agent's own words and clear the goal_seeded marker."""
    goal = (args.goal or getattr(args, "goal_flag", None) or "").strip()
    if not goal:
        print(f"restate requires a non-empty goal string. {_USAGE_HINTS['restate']}", file=sys.stderr)
        return 1
    spec = load_spec(args.root, args.task_id)
    created = False
    if spec is None:
        path, _, created = ensure_spec_scaffold(args.root, args.task_id, goal)
        if not path:
            print(f"Could not create spec at {spec_path(args.root, args.task_id)}.", file=sys.stderr)
            return 1
        spec = load_spec(args.root, args.task_id)
        if spec is None:
            print(f"No spec at {spec_path(args.root, args.task_id)}.", file=sys.stderr)
            return 1
    spec["restated_goal"] = goal
    spec["goal_seeded"] = False
    save_spec(args.root, args.task_id, spec)
    if created:
        print(
            f"spec created at {spec_path(args.root, args.task_id)}; restated_goal set ({len(goal)} chars); goal_seeded cleared."
        )
    else:
        print(f"restated_goal set ({len(goal)} chars); goal_seeded cleared.")
    notify_spec_update(spec, "Goal restated.", surface="stdout_only")
    return 0


def _cmd_doctor_session_env(args: argparse.Namespace) -> int:
    # Always emit a machine-scannable diagnostic for the env-resolved session.
    # This line appears in Bash tool results so probes can validate whether the
    # shell subprocess receives the same session id/env as the hook/prompt scaffold.
    resolved_sid, source = resolve_session_id_with_source(default=None)
    print(f"UNIFABLE_SESSION_RESOLVED={resolved_sid or ''} SOURCE={source}", file=sys.stderr)

    print(format_spec_location(args.root, args.task_id))
    spec = load_spec(args.root, args.task_id)
    if spec is not None:
        print()
        print(format_spec_status(spec))
    else:
        fragmented = _find_fragmented_specs(args.task_id, canonical_project_root(args.root))
        if len(fragmented) > 1:
            print("\nMultiple fragmented specs found for this session (run from project root):")
            for path in fragmented:
                print(f"  {path}")
        else:
            print("\n(no spec file yet)")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    if getattr(args, "doctor_cmd", "") == "session-env":
        return _cmd_doctor_session_env(args)
    print("usage: unifable doctor session-env", file=sys.stderr)
    return 1


def _apply_cli_context(args: argparse.Namespace) -> int | None:
    """Resolve canonical root + session from cwd/env. Return exit code on error, else None."""
    args.root = str(canonical_project_root(os.getcwd()))
    if args.cmd == "contract":
        return None
    args.task_id = resolve_session_id(default=None)
    if args.cmd not in (None, "contract") and not args.task_id:
        print(
            "No session id: set CLAUDE_CODE_SESSION_ID, CODEX_THREAD_ID, or CURSOR_CONVERSATION_ID (Cursor).",
            file=sys.stderr,
        )
        return 1
    return None


def main(argv: list[str] | None = None) -> int:
    parser = _HintingParser(
        prog="unifable",
        description="unifable spec artifact validator and contract helper.",
    )
    sub = parser.add_subparsers(dest="cmd", parser_class=_HintingParser)

    p_contract = sub.add_parser("contract", help="Print pass-conditions for a grade tier (harness/dev).")
    p_contract.add_argument("--grade", default="STANDARD", help="Grade tier: LIGHT, STANDARD, HEAVY.")
    p_contract.add_argument(
        "--require-evidence",
        action="store_true",
        dest="require_evidence",
        help="Include the evidence-gate citation requirements.",
    )

    # title/check are optional flags with a hidden positional fallback so the
    # natural `add-task '<title>' '<check>'` form works and the single-arg
    # attempt yields actionable guidance (see _resolve_title_check) instead of
    # a bare argparse error.
    p_add = sub.add_parser("add-task", help="Append a task to an existing spec.")
    p_add.add_argument("--title")
    p_add.add_argument("--check", help="Runnable command that proves the task.")
    p_add.add_argument("rest", nargs="*", help=argparse.SUPPRESS)

    p_restate = sub.add_parser("restate", help="Restate the goal in your own words (clears goal_seeded).")
    p_restate.add_argument(
        "goal",
        nargs="?",
        help="The intended outcome, restated in your own words (quote if it contains spaces).",
    )
    # Accept the common `--goal` mistake as an alias so it succeeds instead of
    # dead-ending on a top-level unrecognized-argument error.
    p_restate.add_argument("--goal", dest="goal_flag", help=argparse.SUPPRESS)

    p_constraint = sub.add_parser(
        "set-primary",
        help="Set the evidence-backed primary approach task (HEAVY; blocked until frontiers ruled out).",
    )
    p_constraint.add_argument("--title")
    p_constraint.add_argument("--check", help="Runnable command proving primary delivery.")
    p_constraint.add_argument("rest", nargs="*", help=argparse.SUPPRESS)

    p_rejected = sub.add_parser(
        "add-frontier",
        help="Append a frontier approach task to explore (HEAVY needs >=2).",
    )
    p_rejected.add_argument("--title")
    p_rejected.add_argument("--check", help="Runnable exploration check.")
    p_rejected.add_argument("rest", nargs="*", help=argparse.SUPPRESS)

    p_doctor = sub.add_parser("doctor", help="Operator diagnostics.")
    doctor_sub = p_doctor.add_subparsers(dest="doctor_cmd")
    doctor_sub.add_parser("session-env", help="Show canonical spec path and session env diagnostics.")

    args = parser.parse_args(argv)
    err = _apply_cli_context(args)
    if err is not None:
        return err
    dispatch = {
        "contract": _cmd_contract,
        "restate": _cmd_restate,
        "add-task": _cmd_add_task,
        "set-primary": _cmd_set_primary,
        "add-frontier": _cmd_add_frontier,
        "doctor": _cmd_doctor,
    }
    handler = dispatch.get(args.cmd)
    if handler:
        return handler(args)
    parser.print_help()
    return 1
