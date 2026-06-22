#!/usr/bin/env python3
"""Single policy boundary between the prompt classifier and the evidence gates.

Two vocabularies used to be wired straight into the gates:

  - task_mode (quick / normal / deep) — what the prompt classifier and the
    per-turn context nudge speak. A UX/telemetry label.
  - grade (LIGHT / STANDARD / HEAVY) — what the evidence gate enforces. The
    canonical enforcement level.

They map 1:1, but the gates each re-derived and re-read them independently, so
the derived value (grade) was persisted and read in three places and could drift
from its source. This module makes the conversion and the read-time precedence
live in exactly one place:

  - task_mode stays the persisted classification (source of truth, pinned to the
    locked active task by gate_prompt.py).
  - grade is DERIVED from task_mode here, never persisted as an independent
    authority. Legacy ledger['grade'] is read only as a back-compat fallback.

Precedence for the effective grade (resolve_grade):
  1. a valid UNIFABLE_GRADE override (env_grade arg),
  2. the active task's task_mode -> derived grade (only when active_task is set),
  3. legacy ledger['grade'] (old ledgers predating this module),
  4. STANDARD.

Host-agnostic: no Claude-only or Codex-only imports. GRADES is owned by spec.py
(the validation layer); this module imports it rather than minting a second list.
"""

from __future__ import annotations

from typing import Any

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from spec import GRADES
except ImportError:  # pragma: no cover
    from scripts.gate.spec import GRADES

# Classifier labels. Kept here so the mode->grade map has one home.
MODES = ("quick", "normal", "deep")

# The only mode->grade authority. quick waives the spec (LIGHT); normal needs the
# full evidence spec (STANDARD); deep uses frontier-first HEAVY workflow
# (>=2 frontier tasks + 1 primary; see heavy_workflow.py).
MODE_TO_GRADE = {"quick": "LIGHT", "normal": "STANDARD", "deep": "HEAVY"}

DEFAULT_GRADE = "STANDARD"


def grade_for_mode(mode: str | None) -> str:
    """Derive the enforcement grade for a classifier *mode*. Unknown -> STANDARD."""
    return MODE_TO_GRADE.get((mode or "").lower().strip(), DEFAULT_GRADE)


def _norm_grade(value: Any) -> str:
    return (value or "").upper().strip() if isinstance(value, str) else ""


def _mode_rank(mode: str | None) -> int:
    try:
        return MODES.index((mode or "").lower().strip())
    except ValueError:
        return -1


def higher_mode(a: str | None, b: str | None) -> str:
    """Return the more demanding of two classifier modes (quick < normal < deep).

    Used to pin a locked active task's policy: a follow-up prompt may escalate the
    task's rigor but must never lower it while the spec is still open. Unknown
    modes rank below 'quick'; ties and the all-unknown case return *b*."""
    return a if _mode_rank(a) > _mode_rank(b) else b


class Policy:
    """Resolved enforcement decision for the current task.

    grade is always a canonical uppercase string (LIGHT/STANDARD/HEAVY) so the
    existing string-based validate_spec()/contract_string() call sites stay safe.
    """

    __slots__ = ("grade", "task_mode")

    def __init__(self, grade: str, task_mode: str | None = None):
        self.grade = grade if grade in GRADES else DEFAULT_GRADE
        self.task_mode = task_mode

    @property
    def requires_spec(self) -> bool:
        """True when an evidence spec must exist before action/completion.

        LIGHT (quick) waives the spec; STANDARD and HEAVY require it."""
        return self.grade != "LIGHT"

    @property
    def waived(self) -> bool:
        return self.grade == "LIGHT"

    @property
    def blocks_unverified_stop(self) -> bool:
        """True when the softer observation gate should block a changed-but-
        unverified completion. HEAVY only (matches the prior deep-only behavior)."""
        return self.grade == "HEAVY"

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Policy(grade={self.grade!r}, task_mode={self.task_mode!r})"


def policy_for_grade(grade: str, task_mode: str | None = None) -> Policy:
    return Policy(_norm_grade(grade) or DEFAULT_GRADE, task_mode)


def policy_for_mode(mode: str | None) -> Policy:
    return Policy(grade_for_mode(mode), mode)


def resolve_grade(ledger: dict[str, Any] | None, env_grade: Any = None) -> str:
    """Resolve the effective enforcement grade from override + ledger state.

    See the module docstring for the precedence. Never raises: unknown inputs
    fall through to STANDARD so the gate defaults to enforcing, not waiving."""
    env = _norm_grade(env_grade)
    if env in GRADES:
        return env

    ledger = ledger if isinstance(ledger, dict) else {}

    # Derive from the active task's classification. Gating on active_task keeps a
    # fresh/never-classified ledger (no prompt processed yet) at the STANDARD
    # default instead of waiving on the "quick" task_mode default.
    if ledger.get("active_task"):
        mode = (ledger.get("task_mode") or "").lower().strip()
        if mode in MODE_TO_GRADE:
            return MODE_TO_GRADE[mode]

    legacy = _norm_grade(ledger.get("grade"))
    if legacy in GRADES:
        return legacy

    return DEFAULT_GRADE


def resolve_policy(ledger: dict[str, Any] | None, env_grade: Any = None) -> Policy:
    """Resolve the full Policy (grade + task_mode) for the current task."""
    grade = resolve_grade(ledger, env_grade)
    task_mode = ledger.get("task_mode") if isinstance(ledger, dict) else None
    return Policy(grade, task_mode)
