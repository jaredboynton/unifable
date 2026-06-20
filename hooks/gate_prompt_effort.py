#!/usr/bin/env python3
"""unifable effort-gated playbook injection — UserPromptSubmit.

Reads the resolved effort level from the hook's JSON payload or env and injects
the unifable SKILL.md playbook as additionalContext when effort is in
HEAVY_EFFORT. Suppresses re-injection within the same session via a marker file
at <tmpdir>/unifable-loaded-<session_id>. Fails open (emits {} exit 0 on any
error).
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

HEAVY_EFFORT = {"xhigh", "max", "ultracode"}

# Skill file relative to this hook's parent repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SKILL_PATH = _REPO_ROOT / "skills" / "unifable" / "SKILL.md"


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
    try:
        body = _SKILL_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""

    # Extract the "Working style" section onward as a lighter payload when the
    # full body is very long (>6 KB). Always include the routing summary
    # (sections 3-1 onward) so the minimal injection still covers grounding.
    THRESHOLD = 6144
    if len(body) > THRESHOLD:
        # Find "## 3-1. Working style" and emit from there.
        idx = body.find("## 3-1. Working style")
        if idx == -1:
            # Fallback: first 4 KB of the body.
            body = body[:4096] + "\n...(truncated)"
        else:
            body = body[idx:]

    intro = (
        "unifable execution playbook active (effort=heavy). Adopt the discipline "
        "below as standing procedure for the rest of this session:\n\n"
    )
    return intro + body


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
