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
from pathlib import Path

HEAVY_EFFORT = {"xhigh", "max", "ultracode"}

_PLAYBOOK_CORE = """\
High-effort checklist:
- Cite current tool evidence for completion claims.
- For multi-part work, create one spec task per deliverable and validate each.
- If stuck twice, preserve evidence and narrow the failing slice before escalating or delegating.
- For rendered artifacts, run the real renderer and inspect actual output."""

_PLAYBOOK_INVESTIGATION = """\
Investigation: reproduce first. Form 3+ competing hypotheses before \
investigating any single one. Gather evidence per hypothesis by reading code \
paths end to end. Trace the full causal chain. Verify before and after. Report \
the hypotheses you rejected and the evidence that rejected them."""

_PLAYBOOK_GROUNDING = """\
Verification grounding: for artifacts whose correctness only shows when run \
(HTML, SVG, games, UI, charts), run it in the real renderer, observe the actual \
output, fix what the observation reveals, then re-run. A static parse confirms \
well-formed, not correct."""

# Router pack tags that supersede a playbook paragraph.
_TAG_SUPERSEDES = {
    "investigation": _PLAYBOOK_INVESTIGATION,
    "grounding": _PLAYBOOK_GROUNDING,
}


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


def _playbook_context(matched_tags: set[str] | None = None) -> str:
    tags = matched_tags or set()
    parts = [_PLAYBOOK_CORE]
    for tag, paragraph in _TAG_SUPERSEDES.items():
        if tag not in tags:
            parts.append(paragraph)
    return "\n\n".join(parts)


def effort_additional_context(data: dict) -> str | None:
    """Return the effort playbook context to inject, or None.

    Heavy-effort gated, once per session via a marker file, with router-tag
    paragraph suppression. In-process callable used by both this hook's main()
    and the consolidated UserPromptSubmit entrypoint (hooks/gate_prompt.py).
    Returns None for non-heavy effort, an already-injected session, or empty
    context; fails open to None on any error.
    """
    effort = _resolved_effort(data)
    if effort not in HEAVY_EFFORT:
        return None

    session_id = str(data.get("session_id") or "nosession")
    marker = _marker_path(session_id)
    if os.path.exists(marker):
        return None  # already injected this session

    # Create marker before building context so a read error still records dedup.
    try:
        os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
        open(marker, "w").close()  # noqa: WPS515 — intentional touch
    except OSError:
        pass  # fail open: marker write failure must not block injection

    # Suppress playbook paragraphs whose router pack already fired this prompt.
    matched_tags: set[str] = set()
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))
        from ledger import load_ledger

        matched_tags = set(load_ledger(data).get("router_fired_tags") or [])
    except Exception:
        pass

    return _playbook_context(matched_tags) or None


def main() -> int:
    data = _read_stdin_json()
    context = effort_additional_context(data)
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
        _emit({"systemMessage": f"Effort hook failed open: {exc}"})
        raise SystemExit(0)
