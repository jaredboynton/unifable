#!/usr/bin/env python3
"""Spec artifact validator and contract helper for the unifable pre-edit gate.

Provides:
  - SPEC_SCHEMA: field definitions (required and optional)
  - FAKE_MARKERS: tuple of placeholder strings that indicate fabricated evidence
  - validate_spec(spec, grade) -> (ok, reasons)
  - check_fake_evidence(text) -> list[str]
  - spec_path(root, task_id) -> Path
  - load_spec(root, task_id) -> dict | None
  - save_spec(root, task_id, spec) -> Path
  - spec_template() -> dict
  - CLI: validate / init / contract subcommands
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from atomicio import write_text_atomic
except ImportError:  # pragma: no cover
    from scripts.gate.atomicio import write_text_atomic

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SPEC_SCHEMA: dict[str, dict[str, Any]] = {
    # required
    "restated_goal": {
        "type": str,
        "required": True,
        "description": "The goal restated in the model's own words; must differ from raw ask.",
    },
    "acceptance_criteria": {
        "type": list,
        "required": True,
        "description": "List of {check: <runnable command str>, evidence: <observed output>}.",
    },
    # optional
    "risks": {
        "type": list,
        "required": False,
        "description": "List of risks with blast-radius and mitigation.",
    },
    "constraints": {
        "type": list,
        "required": False,
        "description": "Architectural or operational constraints that bound the solution.",
    },
    "rejected_alternatives": {
        "type": list,
        "required": False,
        "description": "Approaches considered and rejected; each should state the broken boundary.",
    },
    "non_goals": {
        "type": list,
        "required": False,
        "description": "What is explicitly out of scope.",
    },
    # evidence-gate citation fields (required only when require_evidence=True)
    "must_read": {
        "type": list,
        "required": False,
        "description": "CODE evidence: 'path:line' citations the model actually read before deciding.",
    },
    "prior_art": {
        "type": list,
        "required": False,
        "description": "RESEARCH evidence: source URLs (docs/repos/papers) backing the chosen approach.",
    },
    # CLI-managed task list. Each task carries a runnable `check`; a task becomes
    # `validated` only when the check runs AND the codex judge confirms the output
    # actually satisfies it. When a spec declares tasks, completion (Stop gate)
    # requires EVERY task validated. Authored and mutated only via spec.py CLI.
    "tasks": {
        "type": list,
        "required": False,
        "description": "List of {id, title, check, status, exit, output, judge_verdict, judge_reason}.",
    },
}

# Grade tier requirements:
#   LIGHT    — restated_goal + >=1 acceptance_criteria (waives spec for trivial changes)
#   STANDARD — full required set (restated_goal + acceptance_criteria)
#   HEAVY    — STANDARD + >=1 constraints + >=2 rejected_alternatives
GRADES = ("LIGHT", "STANDARD", "HEAVY")

# ---------------------------------------------------------------------------
# Fake-evidence detection
# ---------------------------------------------------------------------------

FAKE_MARKERS: tuple[str, ...] = (
    "not run",
    "assumed",
    "assumption",
    "(assumption)",
    "i assume",
    "presumably",
    "would pass",
    "will pass",
    "should pass",
    "tbd",
    "pending",
    "n/a",
    "todo",
    "will run",
    "placeholder",
    "to be determined",
    "not tested",
    "not verified",
    "not checked",
    "skipped",
    "manually verified",
    "manually tested",
    "trust me",
    "obviously works",
)


def check_fake_evidence(text: str) -> list[str]:
    """Return any FAKE_MARKERS found (case-insensitive) in *text*.

    Used to reject acceptance_criteria evidence fields that contain placeholder
    language rather than live command output.
    """
    lower = (text or "").lower()
    return [marker for marker in FAKE_MARKERS if marker in lower]


# ---------------------------------------------------------------------------
# Citation-format detection (evidence gate)
# ---------------------------------------------------------------------------

# A 'path:line' or 'path:start-end' code citation (e.g. src/app.py:42, a/b.py:10-20).
_PATH_LINE_RE = re.compile(r"^.+:\d+(?:-\d+)?$")
# A source URL.
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)


def is_path_line(s: str) -> bool:
    """True when *s* looks like a 'path:line' code citation (not a URL)."""
    if not isinstance(s, str):
        return False
    s = s.strip()
    if s.lower().startswith(("http://", "https://")):
        return False
    return bool(_PATH_LINE_RE.match(s))


def is_source_url(s: str) -> bool:
    """True when *s* is an http(s) URL."""
    return isinstance(s, str) and bool(_URL_RE.match(s.strip()))


def must_read_parts(item: Any) -> tuple[str, str]:
    """Return (cite, why) for a must_read entry.

    Accepts the required object form {'cite': 'path:line', 'why': '<why relevant>'}.
    A bare 'path:line' string yields (string, '') so the missing-why check fires."""
    if isinstance(item, dict):
        return str(item.get("cite") or item.get("path") or ""), str(item.get("why") or "")
    if isinstance(item, str):
        return item, ""
    return "", ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_spec(
    spec: dict[str, Any], grade: str, require_evidence: bool = False
) -> tuple[bool, list[str]]:
    """Validate *spec* against the requirements for *grade*.

    When *require_evidence* is True (how the hooks always call it), the spec must
    also carry citation evidence at STANDARD+: 'must_read' (>=1 {cite: 'path:line',
    why: '<why relevant>'}) and 'prior_art' (>=1 source URL). This makes the spec
    the documented, verifiable evidence that unlocks action.

    Returns (ok, reasons) where reasons is empty when ok is True.
    """
    grade = (grade or "STANDARD").upper()
    if grade not in GRADES:
        return False, [f"Unknown grade '{grade}'; expected one of {', '.join(GRADES)}."]

    reasons: list[str] = []

    if not isinstance(spec, dict):
        return False, ["Spec must be a JSON object."]

    # restated_goal — required for all grades
    goal = spec.get("restated_goal")
    if not goal or not isinstance(goal, str) or not goal.strip():
        reasons.append("'restated_goal' is required and must be a non-empty string.")

    # acceptance_criteria — required for all grades, >=1 item with a non-empty check.
    # A task-spec (CLI-authored, has >=1 task with a check) satisfies this instead:
    # the tasks ARE the acceptance criteria, and their live evidence is produced at
    # validate-task time (judged), not at authoring time.
    tasks = spec.get("tasks")
    has_tasks = isinstance(tasks, list) and any(
        isinstance(t, dict) and str(t.get("check", "")).strip() for t in tasks
    )
    criteria = spec.get("acceptance_criteria")
    if has_tasks:
        pass  # tasks stand in for acceptance_criteria
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

    # HEAVY requires >=1 constraints and >=2 rejected_alternatives
    if grade == "HEAVY":
        constraints = spec.get("constraints")
        if not isinstance(constraints, list) or not constraints:
            reasons.append("HEAVY grade requires 'constraints' (list, >=1 item).")

        rejected = spec.get("rejected_alternatives")
        if not isinstance(rejected, list) or len(rejected) < 2:
            reasons.append("HEAVY grade requires 'rejected_alternatives' (list, >=2 items).")

    # Evidence gate: citation fields become required at STANDARD+ (LIGHT is exempt
    # because LIGHT waives the spec entirely upstream). Each must_read citation must
    # carry a 'why relevant' rationale, and prior_art (research/frontier evidence) is
    # required from STANDARD up.
    if require_evidence and grade in ("STANDARD", "HEAVY"):
        must_read = spec.get("must_read")
        if not isinstance(must_read, list) or not must_read:
            reasons.append(
                "evidence gate: 'must_read' is required (list, >=1 "
                "{cite: 'path:line', why: 'why this passage is relevant'})."
            )
        else:
            for idx, item in enumerate(must_read):
                cite, why = must_read_parts(item)
                if not is_path_line(cite):
                    reasons.append(
                        f"must_read[{idx}].cite must be a 'path:line' code citation "
                        f"(e.g. src/app.py:42), got {item!r}."
                    )
                elif check_fake_evidence(cite):
                    reasons.append(
                        f"must_read[{idx}].cite is an unproven assumption/placeholder ({cite!r}). "
                        "The gate rejects assumptions -- cite a real path:line you read."
                    )
                if not why.strip():
                    reasons.append(
                        f"must_read[{idx}] needs a non-empty 'why' (why the passage is relevant); "
                        f"use {{'cite': '{cite or 'path:line'}', 'why': '...'}}."
                    )
                elif check_fake_evidence(why):
                    reasons.append(
                        f"must_read[{idx}].why is an unproven assumption/placeholder ({why!r}). "
                        "The gate rejects assumptions -- prove why the passage is relevant."
                    )

        # prior_art — required from STANDARD up (source URLs for prior art / frontier research)
        prior_art = spec.get("prior_art")
        if not isinstance(prior_art, list) or not prior_art:
            reasons.append(
                "evidence gate: 'prior_art' is required (list, >=1 source URL for prior art "
                "/ frontier research backing the approach)."
            )
        else:
            for idx, item in enumerate(prior_art):
                if not is_source_url(item):
                    reasons.append(
                        f"prior_art[{idx}] must be a source URL (http(s)://...), got {item!r}."
                    )

    return not reasons, reasons


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def resolve_session_id(input_data: dict | None = None, default: str | None = "default") -> str | None:
    """Resolve the per-session key for spec artifacts, consistent across hosts.

    Precedence:
      1. explicit ``session_id`` in the hook payload (Claude Code sends it on
         stdin) -- keeps Claude Code behaviour unchanged,
      2. ``CLAUDE_CODE_SESSION_ID`` (Claude Code env),
      3. ``CODEX_THREAD_ID`` (Codex env),
      4. *default*.

    Hosts that omit ``session_id`` from the hook payload (Codex) and CLI tools
    with no stdin still key the spec per conversation via the env vars both
    runtimes export, instead of colliding on one shared file. Callers that want
    to fail open when nothing resolves pass ``default=None``.
    """
    if input_data:
        sid = input_data.get("session_id")
        if sid:
            return str(sid)
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        val = os.environ.get(var)
        if val:
            return val
    return default


def spec_path(root: str | Path, task_id: str) -> Path:
    """Return the canonical path for a spec artifact: <root>/.unifable/spec/<task_id>.json"""
    return Path(root).resolve() / ".unifable" / "spec" / f"{task_id}.json"


def load_spec(root: str | Path, task_id: str) -> dict[str, Any] | None:
    """Load and parse a spec artifact, returning None on any error."""
    path = spec_path(root, task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def save_spec(root: str | Path, task_id: str, spec: dict[str, Any]) -> Path:
    """Write *spec* to the canonical path, creating parent directories as needed."""
    path = spec_path(root, task_id)
    return write_text_atomic(path, json.dumps(spec, indent=2, sort_keys=False))


# ---------------------------------------------------------------------------
# Task model (CLI-managed, judge-validated)
# ---------------------------------------------------------------------------

_OUTPUT_LIMIT = 4000  # cap on captured check output stored in the spec


def find_task(spec: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for t in spec.get("tasks") or []:
        if isinstance(t, dict) and str(t.get("id")) == str(task_id):
            return t
    return None


def all_tasks_validated(spec: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return (ok, incomplete_ids). ok is True when the spec declares >=1 task and
    EVERY task is 'validated'. A spec with no tasks returns (True, []) so legacy
    acceptance-criteria specs are unaffected (the evidence gate still applies)."""
    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return True, []
    incomplete = [
        str(t.get("id")) for t in tasks
        if not (isinstance(t, dict) and t.get("status") == "validated")
    ]
    return (not incomplete), incomplete


def run_check(check: str, cwd: str | Path = ".", timeout: int = 600) -> tuple[int, str]:
    """Run a task's check command -> (exit_code, combined stdout+stderr, capped)."""
    try:
        proc = subprocess.run(
            check, shell=True, cwd=str(cwd),
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out[-_OUTPUT_LIMIT:]
    except subprocess.TimeoutExpired:
        return 124, f"(check timed out after {timeout}s)"
    except Exception as exc:  # noqa: BLE001
        return 127, f"(check failed to run: {exc})"


_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "integer", "enum": [0, 1]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


def judge_task(spec: dict[str, Any], task: dict[str, Any], exit_code: int, output: str) -> tuple[int, str]:
    """Ask the codex judge whether the check output actually validates the task.

    Returns (verdict, reason); verdict is 1 only when the model is convinced the
    output genuinely demonstrates the task is done and correct. Any judge failure
    returns (0, reason) so an unreachable judge never auto-passes a task."""
    try:
        from codex_judge import JudgeError, ask_structured
    except ImportError as exc:  # pragma: no cover
        return 0, f"judge unavailable: {exc}"
    system = (
        "You are a strict, adversarial validator for a software task. You are given "
        "the overall goal, one task with its check command, the command's exit code, "
        "and its captured output. Decide whether the output is real evidence that the "
        "task is genuinely complete and correct -- not merely that a command ran. "
        "Return verdict 1 only if convinced; otherwise 0. Be skeptical of empty "
        "output, errors, skipped or zero tests, and output that does not match the task."
    )
    user = json.dumps({
        "goal": spec.get("restated_goal", ""),
        "task_title": task.get("title", ""),
        "check": task.get("check", ""),
        "exit_code": exit_code,
        "output": output,
    }, ensure_ascii=False)
    try:
        res = ask_structured(system, user, _JUDGE_SCHEMA, schema_name="task_verdict")
    except JudgeError as exc:
        return 0, f"judge error: {exc}"
    return (1 if res.get("verdict") == 1 else 0), str(res.get("reason") or "")


def spec_template() -> dict[str, Any]:
    """Return an empty spec scaffold the model can fill in."""
    return {
        "restated_goal": "",
        "acceptance_criteria": [
            {"check": "", "evidence": ""}
        ],
        "must_read": [
            {"cite": "", "why": ""}
        ],
        "prior_art": [],
        "risks": [],
        "constraints": [],
        "rejected_alternatives": [],
        "non_goals": [],
    }


# ---------------------------------------------------------------------------
# Grade contract strings
# ---------------------------------------------------------------------------

_CONTRACT: dict[str, str] = {
    "LIGHT": (
        "unifable spec contract — LIGHT grade. "
        "Before editing: write .unifable/spec/<task-id>.json with 'restated_goal' (non-empty string) "
        "and 'acceptance_criteria' (list with >=1 {check, evidence} entry). "
        "Evidence must be live command output — no placeholders."
    ),
    "STANDARD": (
        "unifable spec contract — STANDARD grade. "
        "Before editing: write .unifable/spec/<task-id>.json with 'restated_goal' "
        "and 'acceptance_criteria' (>=1 {check: <runnable command>, evidence: <live output>}). "
        "Evidence must be observed tool output, not assumed."
    ),
    "HEAVY": (
        "unifable spec contract — HEAVY grade. "
        "Before editing: write .unifable/spec/<task-id>.json with 'restated_goal', "
        "'acceptance_criteria' (>=1 {check, evidence} with live output), "
        "'constraints' (>=1 architectural constraint), "
        "and 'rejected_alternatives' (>=2 entries each stating the broken boundary). "
        "No placeholder evidence — every criteria item must carry observed command output."
    ),
}


def contract_string(grade: str, require_evidence: bool = False) -> str:
    """Return the pass-conditions for *grade* as a short additionalContext string.

    When *require_evidence* is True, append the evidence-gate citation requirements
    (must_read with why-rationale + prior_art, both at STANDARD+).
    """
    grade = (grade or "STANDARD").upper()
    base = _CONTRACT.get(grade, _CONTRACT["STANDARD"])
    if require_evidence and grade != "LIGHT":
        base = base + (
            " Evidence gate: also include 'must_read' (>=1 {cite:'path:line', why:'why it's "
            "relevant'}) and 'prior_art' (>=1 source URL for prior art / frontier research)."
        )
    return base


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_validate(args: argparse.Namespace) -> int:
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        print(
            f"No spec found at {spec_path(args.root, args.task_id)}. "
            "Run 'spec.py init' to create a template.",
            file=sys.stderr,
        )
        return 1
    grade = (args.grade or "STANDARD").upper()
    ok, reasons = validate_spec(spec, grade, require_evidence=getattr(args, "require_evidence", False))
    if ok:
        print(f"spec valid (grade={grade})")
        return 0
    for reason in reasons:
        print(f"- {reason}")
    return 1


def _cmd_init(args: argparse.Namespace) -> int:
    path = spec_path(args.root, args.task_id)
    if path.exists():
        print(f"Spec already exists at {path}; not overwriting.", file=sys.stderr)
        return 1
    save_spec(args.root, args.task_id, spec_template())
    print(f"Spec template written to {path}")
    return 0


def _cmd_contract(args: argparse.Namespace) -> int:
    grade = (args.grade or "STANDARD").upper()
    if grade not in GRADES:
        print(f"Unknown grade '{grade}'; expected one of {', '.join(GRADES)}.", file=sys.stderr)
        return 1
    print(contract_string(grade, getattr(args, "require_evidence", False)))
    return 0


def _next_task_id(spec: dict[str, Any]) -> str:
    return f"T{len(spec.get('tasks') or []) + 1}"


def _new_task(spec: dict[str, Any], title: str, check: str) -> dict[str, Any]:
    return {
        "id": _next_task_id(spec), "title": title.strip(), "check": check.strip(),
        "status": "pending", "exit": None, "output": "",
        "judge_verdict": None, "judge_reason": "",
    }


def _cmd_create(args: argparse.Namespace) -> int:
    path = spec_path(args.root, args.task_id)
    if path.exists() and not getattr(args, "force", False):
        print(f"Spec already exists at {path}; use --force to replace.", file=sys.stderr)
        return 1
    spec = spec_template()
    spec["restated_goal"] = args.goal
    spec["acceptance_criteria"] = []  # tasks stand in for acceptance_criteria
    spec["must_read"] = []
    spec["prior_art"] = list(getattr(args, "prior_art", None) or [])
    spec["constraints"] = list(getattr(args, "constraint", None) or [])
    spec["rejected_alternatives"] = list(getattr(args, "rejected", None) or [])
    spec["tasks"] = []
    for mr in (getattr(args, "must_read", None) or []):
        cite, _sep, why = mr.partition("::")
        spec["must_read"].append({"cite": cite.strip(), "why": why.strip()})
    for pair in (args.task or []):
        if "::" not in pair:
            print(f"--task must be 'title::check command' -- invalid: {pair}", file=sys.stderr)
            return 1
        title, check = pair.split("::", 1)
        spec["tasks"].append(_new_task(spec, title, check))
    save_spec(args.root, args.task_id, spec)
    print(f"Spec created at {path} with {len(spec['tasks'])} task(s).")
    return 0


def _cmd_add_task(args: argparse.Namespace) -> int:
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        print(f"No spec at {spec_path(args.root, args.task_id)}; run 'create' first.", file=sys.stderr)
        return 1
    spec.setdefault("tasks", [])
    task = _new_task(spec, args.title, args.check)
    spec["tasks"].append(task)
    save_spec(args.root, args.task_id, spec)
    print(f"Added {task['id']}: {task['title']}")
    return 0


def _cmd_deliver(args: argparse.Namespace) -> int:
    spec = load_spec(args.root, args.task_id)
    task = find_task(spec, args.task) if spec else None
    if task is None:
        print(f"Task {args.task} not found.", file=sys.stderr)
        return 1
    if task.get("status") != "validated":
        task["status"] = "delivered"
        save_spec(args.root, args.task_id, spec)
    print(f"{args.task} -> {task['status']}")
    return 0


def _cmd_validate_task(args: argparse.Namespace) -> int:
    spec = load_spec(args.root, args.task_id)
    task = find_task(spec, args.task) if spec else None
    if task is None:
        print(f"Task {args.task} not found.", file=sys.stderr)
        return 1
    exit_code, output = run_check(task.get("check", ""), cwd=args.root)
    verdict, reason = judge_task(spec, task, exit_code, output)
    task["exit"] = exit_code
    task["output"] = output
    task["judge_verdict"] = verdict
    task["judge_reason"] = reason
    task["status"] = "validated" if verdict == 1 else "failed"
    save_spec(args.root, args.task_id, spec)
    print(f"{args.task}: check exit={exit_code}; judge verdict={verdict} ({reason})")
    print(f"{args.task} -> {task['status']}")
    return 0 if verdict == 1 else 2


def _cmd_status(args: argparse.Namespace) -> int:
    spec = load_spec(args.root, args.task_id)
    if spec is None:
        print(f"No spec at {spec_path(args.root, args.task_id)}.", file=sys.stderr)
        return 1
    ok, incomplete = all_tasks_validated(spec)
    print(f"goal: {str(spec.get('restated_goal', ''))[:100]}")
    mark = {"validated": "OK", "failed": "XX", "delivered": "..", "pending": "--"}
    for t in spec.get("tasks") or []:
        print(f"  [{mark.get(t.get('status'), '??')}] {t.get('id')} {t.get('title')}")
    print("breaker: OPEN (all tasks validated)" if ok
          else f"breaker: CLOSED ({len(incomplete)} left: {', '.join(incomplete)})")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spec.py",
        description="unifable spec artifact validator and contract helper.",
    )
    sub = parser.add_subparsers(dest="cmd")

    p_validate = sub.add_parser("validate", help="Validate an existing spec.")
    p_validate.add_argument("--root", default=".", help="Project root (default: cwd).")
    p_validate.add_argument("--grade", default="STANDARD", help="Grade tier: LIGHT, STANDARD, HEAVY.")
    p_validate.add_argument("--task-id", required=True, dest="task_id", help="Task ID for the spec file.")
    p_validate.add_argument("--require-evidence", action="store_true", dest="require_evidence",
                            help="Also require citation evidence (must_read, prior_art).")

    p_init = sub.add_parser("init", help="Write a blank spec template.")
    p_init.add_argument("--root", default=".", help="Project root (default: cwd).")
    p_init.add_argument("--task-id", required=True, dest="task_id", help="Task ID for the spec file.")

    p_contract = sub.add_parser("contract", help="Print pass-conditions for a grade tier.")
    p_contract.add_argument("--grade", default="STANDARD", help="Grade tier: LIGHT, STANDARD, HEAVY.")
    p_contract.add_argument("--require-evidence", action="store_true", dest="require_evidence",
                            help="Include the evidence-gate citation requirements.")

    p_create = sub.add_parser("create", help="Create a task spec (restated_goal + tasks).")
    p_create.add_argument("--root", default=".")
    p_create.add_argument("--task-id", required=True, dest="task_id")
    p_create.add_argument("--goal", required=True, help="Restated goal in your own words.")
    p_create.add_argument("--task", action="append", default=[], help="Task as 'title::check command' (repeatable).")
    p_create.add_argument("--must-read", action="append", default=[], dest="must_read",
                          help="Evidence citation 'path:line::why' (repeatable).")
    p_create.add_argument("--prior-art", action="append", default=[], dest="prior_art",
                          help="Source URL backing the approach (repeatable).")
    p_create.add_argument("--constraint", action="append", default=[],
                          help="Architectural/operational constraint (repeatable; >=1 required at HEAVY).")
    p_create.add_argument("--rejected", action="append", default=[],
                          help="Rejected alternative with reason (repeatable; >=2 required at HEAVY).")
    p_create.add_argument("--force", action="store_true", help="Replace an existing spec.")

    p_add = sub.add_parser("add-task", help="Append a task to an existing spec.")
    p_add.add_argument("--root", default=".")
    p_add.add_argument("--task-id", required=True, dest="task_id")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--check", required=True, help="Runnable command that proves the task.")

    p_deliver = sub.add_parser("deliver", help="Mark a task delivered (code written).")
    p_deliver.add_argument("--root", default=".")
    p_deliver.add_argument("--task-id", required=True, dest="task_id")
    p_deliver.add_argument("--task", required=True, help="Task id, e.g. T1.")

    p_vt = sub.add_parser("validate-task", help="Run a task's check + judge it; mark validated/failed.")
    p_vt.add_argument("--root", default=".")
    p_vt.add_argument("--task-id", required=True, dest="task_id")
    p_vt.add_argument("--task", required=True, help="Task id, e.g. T1.")

    p_status = sub.add_parser("status", help="Show task statuses + breaker state.")
    p_status.add_argument("--root", default=".")
    p_status.add_argument("--task-id", required=True, dest="task_id")

    args = parser.parse_args(argv)
    dispatch = {
        "validate": _cmd_validate, "init": _cmd_init, "contract": _cmd_contract,
        "create": _cmd_create, "add-task": _cmd_add_task, "deliver": _cmd_deliver,
        "validate-task": _cmd_validate_task, "status": _cmd_status,
    }
    handler = dispatch.get(args.cmd)
    if handler:
        return handler(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
