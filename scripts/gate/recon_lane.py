#!/usr/bin/env python3
"""gpt-realtime-mini recon + execution lane for the gpt-realtime-2 judge.

mini is NEVER a decision-maker here. It is gpt-realtime-2's parallel lane for two
host-gated, fail-open jobs:

  1. run_validation_command(cmd, cwd): gpt-realtime-2 AUTHORS a read-only command;
     the host gates it through the existing read-only allowlist
     (bash_classify.is_allowed_research_bash) and runs it via the Stop gate's
     run_check. mini/this lane contributes ZERO judgment -- only execution and
     captured (exit_code, output). A disallowed command never runs.

  2. recon_gather(questions, cwd, model): fan out read-only context questions to
     the consolidated daemon's mini pool; each returns a structured observation
     ({found, where, note}); the host coalesces them into one evidence blob for
     gpt-realtime-2. mini only reports what it saw; it does not rank, decide, or
     propose a command.

Every path is fail-open: a disallowed command, a daemon miss, a timeout, or any
error yields an inert result so the existing gpt-realtime-2-only behavior stands.

Stdlib only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# The recon/exec lane model. mini is fast/cheap and only ever executes or reports;
# it never adjudicates. Override with UNIFABLE_RECON_MODEL.
RECON_MODEL = os.environ.get("UNIFABLE_RECON_MODEL", "gpt-realtime-mini").strip() or "gpt-realtime-mini"

_RECON_MAX_QUESTIONS = 8
_RECON_OUTPUT_LIMIT = 4000


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


RECON_CMD_TIMEOUT = _env_float("UNIFABLE_RECON_CMD_TIMEOUT", 60.0)


def run_validation_command(cmd: str, cwd: str | Path = ".") -> dict[str, Any]:
    """Run a gpt-realtime-2-AUTHORED read-only command on the recon/exec lane.

    Returns {ran, allowed, exit_code, output, reason}. The host gates ``cmd``
    through the read-only allowlist FIRST; a disallowed command is never executed
    (ran=False, allowed=False, exit_code=None). This is pure execution + capture:
    no judgment is produced here, and the deterministic exit code is what an
    upstream gpt-realtime-2 gate uses to decide -- never a mini opinion."""
    text = str(cmd or "").strip()
    if not text:
        return {"ran": False, "allowed": False, "exit_code": None, "output": "", "reason": "empty command"}
    try:
        from bash_classify import is_allowed_research_bash

        allowed, reason = is_allowed_research_bash(text)
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "allowed": False, "exit_code": None, "output": "", "reason": f"gate error: {exc}"}
    if not allowed:
        return {"ran": False, "allowed": False, "exit_code": None, "output": "", "reason": reason or "not read-only"}
    try:
        from spec_stop_validate import run_check

        exit_code, output = run_check(text, cwd, timeout=int(RECON_CMD_TIMEOUT))
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "allowed": True, "exit_code": None, "output": "", "reason": f"run error: {exc}"}
    return {
        "ran": True,
        "allowed": True,
        "exit_code": exit_code,
        "output": (output or "")[:_RECON_OUTPUT_LIMIT],
        "reason": "",
    }


_RECON_SYSTEM = (
    "You are a read-only recon scout for a code repository. You answer ONE narrow "
    "question about what exists in the repo from the context provided. You never "
    "make decisions, never judge correctness, never propose commands, and never "
    "claim anything you cannot point to. Report only what you can observe."
)

_RECON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "found": {"type": "boolean"},
        "where": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["found", "where", "note"],
}


def recon_gather(
    questions: list[str],
    cwd: str | Path = ".",
    *,
    model: str = RECON_MODEL,
    context: str = "",
    input_data: dict | None = None,
) -> list[dict[str, Any]]:
    """Fan out read-only recon questions to the mini lane; coalesce observations.

    Returns a list aligned to ``questions``; each entry is
    {question, found, where, note} or {question, error} on a failed slot. Fully
    fail-open: with no bound session, a daemon miss, or any error, the slot is
    returned with error set and the caller proceeds without the extra context.
    mini only reports observations; it never decides or proposes a command."""
    qs = [str(q or "").strip() for q in (questions or []) if str(q or "").strip()][:_RECON_MAX_QUESTIONS]
    if not qs:
        return []
    try:
        from judge_transport import ask_structured
    except Exception:
        return [{"question": q, "error": "transport unavailable"} for q in qs]

    cwd_s = str(cwd or ".")
    results: list[dict[str, Any]] = []
    for q in qs:
        user = q if not context else f"{q}\n\n[repo context]\n{context}"
        user = f"cwd: {cwd_s}\n\n{user}"
        try:
            obj = ask_structured(
                _RECON_SYSTEM,
                user,
                _RECON_SCHEMA,
                schema_name="recon",
                model=model,
            )
        except Exception as exc:  # noqa: BLE001
            results.append({"question": q, "error": str(exc)})
            continue
        if not isinstance(obj, dict):
            results.append({"question": q, "error": "no structured result"})
            continue
        results.append(
            {
                "question": q,
                "found": bool(obj.get("found")),
                "where": str(obj.get("where") or "")[:500],
                "note": str(obj.get("note") or "")[:1000],
            }
        )
    return results


def coalesce_recon(results: list[dict[str, Any]]) -> str:
    """Fold recon observations into a compact evidence blob for gpt-realtime-2.

    Pure formatting -- no ranking or judgment. Slots with errors are omitted."""
    lines: list[str] = []
    for r in results or []:
        if not isinstance(r, dict) or r.get("error"):
            continue
        q = str(r.get("question") or "").strip()
        mark = "found" if r.get("found") else "not found"
        where = str(r.get("where") or "").strip()
        note = str(r.get("note") or "").strip()
        piece = f"- {q}: {mark}"
        if where:
            piece += f" @ {where}"
        if note:
            piece += f" -- {note}"
        lines.append(piece)
    return "\n".join(lines)
