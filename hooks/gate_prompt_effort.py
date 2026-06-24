#!/usr/bin/env python3
"""unifable effort-gated playbook injection — UserPromptSubmit.

Injects the unifable heavy-effort playbook as additionalContext when effort is
in HEAVY_EFFORT. Suppresses re-injection within the same session via a marker
file at <tmpdir>/unifable-loaded-<session_id>. Fails open (emits {} exit 0 on
any error).
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile

HEAVY_EFFORT = {"xhigh", "max", "ultracode"}

_PLAYBOOK = """\
unifable execution playbook active (effort=heavy). Adopt the discipline below as \
standing procedure for the rest of this session:

Working style: Lead with the outcome. Stay within the requested scope (no \
incidental refactors or abstractions). Ground every completion claim in a tool \
result from this session. Confirm before destructive or hard-to-reverse actions.

Investigation: reproduce first. Form 3+ competing hypotheses before \
investigating any single one. Gather evidence per hypothesis by reading code \
paths end to end. Trace the full causal chain. Verify before and after. Report \
the hypotheses you rejected and the evidence that rejected them.

Verification grounding: for artifacts whose correctness only shows when run \
(HTML, SVG, games, UI, charts), run it in the real renderer, observe the actual \
output, fix what the observation reveals, then re-run. A static parse confirms \
well-formed, not correct.

Multi-story loop: for 2+ sequential stories, use goals.py to decompose, \
complete one at a time, and produce evidence at each checkpoint. The final story \
must carry --verify-cmd and --verify-evidence.

Escalation: when stuck on the same problem 2+ times, or when the task requires \
out-of-spec discovery, escalate: recommend /effort xhigh, delegate the stuck \
slice via Agent/Workflow with the full evidence package, or hand off with the \
evidence package. Report the limit honestly."""


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _read_stdin_json() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _resolved_effort(data: dict) -> str:
    eff = data.get("effort")
    if isinstance(eff, dict):
        eff = eff.get("level")
    if not eff:
        eff = os.environ.get("CLAUDE_EFFORT") or os.environ.get("UNIFABLE_EFFORT") or ""
    return str(eff).strip().lower()


def _marker_dir() -> str:
    # Allow tests to override via env so markers land in a tmp dir per test run.
    return os.environ.get("UNIFABLE_MARKER_DIR") or tempfile.gettempdir()


def _marker_path(session_id: str) -> str:
    safe_sid = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    return os.path.join(_marker_dir(), f"unifable-loaded-{safe_sid}")


def _playbook_context() -> str:
    return _PLAYBOOK


def main() -> int:
    data = _read_stdin_json()
    effort = _resolved_effort(data)

    if effort not in HEAVY_EFFORT:
        _emit({})
        return 0

    session_id = str(data.get("session_id") or "nosession")
    marker = _marker_path(session_id)

    if os.path.exists(marker):
        # Already injected this session.
        _emit({})
        return 0

    # Create marker before building context so a read error still records dedup.
    try:
        os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
        open(marker, "w").close()  # noqa: WPS515 — intentional touch
    except OSError:
        pass  # fail open: marker write failure must not block injection

    context = _playbook_context()
    if not context:
        _emit({})
        return 0

    _emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 — fail open
        _emit({"systemMessage": f"unifable effort hook failed open: {exc}"})
        raise SystemExit(0)
