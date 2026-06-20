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
import sys
from pathlib import Path
from typing import Any

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
# Validation
# ---------------------------------------------------------------------------

def validate_spec(spec: dict[str, Any], grade: str) -> tuple[bool, list[str]]:
    """Validate *spec* against the requirements for *grade*.

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

    # acceptance_criteria — required for all grades, >=1 item with a non-empty check
    criteria = spec.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
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
                        f"acceptance_criteria[{idx}].evidence contains placeholder language: {fakes}. "
                        "Provide live command output."
                    )

    # HEAVY requires >=1 constraints and >=2 rejected_alternatives
    if grade == "HEAVY":
        constraints = spec.get("constraints")
        if not isinstance(constraints, list) or not constraints:
            reasons.append("HEAVY grade requires 'constraints' (list, >=1 item).")

        rejected = spec.get("rejected_alternatives")
        if not isinstance(rejected, list) or len(rejected) < 2:
            reasons.append("HEAVY grade requires 'rejected_alternatives' (list, >=2 items).")

    return not reasons, reasons


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(spec, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    return path


def spec_template() -> dict[str, Any]:
    """Return an empty spec scaffold the model can fill in."""
    return {
        "restated_goal": "",
        "acceptance_criteria": [
            {"check": "", "evidence": ""}
        ],
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


def contract_string(grade: str) -> str:
    """Return the pass-conditions for *grade* as a short additionalContext string."""
    grade = (grade or "STANDARD").upper()
    return _CONTRACT.get(grade, _CONTRACT["STANDARD"])


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
    ok, reasons = validate_spec(spec, grade)
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
    print(contract_string(grade))
    return 0


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

    p_init = sub.add_parser("init", help="Write a blank spec template.")
    p_init.add_argument("--root", default=".", help="Project root (default: cwd).")
    p_init.add_argument("--task-id", required=True, dest="task_id", help="Task ID for the spec file.")

    p_contract = sub.add_parser("contract", help="Print pass-conditions for a grade tier.")
    p_contract.add_argument("--grade", default="STANDARD", help="Grade tier: LIGHT, STANDARD, HEAVY.")

    args = parser.parse_args(argv)
    if args.cmd == "validate":
        return _cmd_validate(args)
    if args.cmd == "init":
        return _cmd_init(args)
    if args.cmd == "contract":
        return _cmd_contract(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
