#!/usr/bin/env python3
"""Merge unifable's hook entries into a Codex hooks.json, preserving every
non-unifable hook. Idempotent: strips any prior unifable/fablize entries first,
so re-running (or migrating off the legacy fablize port) is safe.

Usage: merge_hooks.py <path-to-hooks.json>
"""
from __future__ import annotations

import json
import os
import sys

BASE = "~/.codex/skills/unifable/hooks"

# unifable's own hook entries, keyed by Codex event name.
UNIFABLE = {
    "UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": f"bash {BASE}/router.sh",
                    "statusMessage": "unifable: routing task signal to pack", "timeout": 10}]},
        {"hooks": [{"type": "command", "command": f"python3 {BASE}/gate_prompt.py",
                    "statusMessage": "unifable: classifying task mode", "timeout": 10}]},
        {"hooks": [{"type": "command", "command": f"python3 {BASE}/gate_prompt_effort.py",
                    "statusMessage": "unifable: effort-gated playbook injection", "timeout": 10}]},
    ],
    "PreToolUse": [
        {"matcher": "^(Edit|Write|MultiEdit|NotebookEdit|apply_patch)$",
         "hooks": [{"type": "command", "command": f"python3 {BASE}/pre_tool_use.py",
                    "statusMessage": "unifable: pre-edit spec gate", "timeout": 10}]},
    ],
    "PostToolUse": [
        {"matcher": "^(Bash|apply_patch)$",
         "hooks": [{"type": "command", "command": f"python3 {BASE}/gate_post_tool.py",
                    "statusMessage": "unifable: observing tool evidence", "timeout": 10}]},
        {"matcher": "^(Edit|Write|MultiEdit|NotebookEdit|apply_patch)$",
         "hooks": [{"type": "command", "command": f"python3 {BASE}/test_after_edit.py",
                    "statusMessage": "unifable: test-after-edit", "timeout": 75}]},
    ],
    "Stop": [
        {"hooks": [{"type": "command", "command": f"python3 {BASE}/gate_stop.py",
                    "statusMessage": "unifable: completion verification gate", "timeout": 10}]},
        {"hooks": [{"type": "command", "command": f"bash {BASE}/finish-the-work.sh",
                    "statusMessage": "unifable: promise-no-act guard", "timeout": 10}]},
    ],
}


def _is_ours(group: object) -> bool:
    """A matcher-group belongs to unifable/fablize if any hook command points at
    the unifable or (legacy) fablize skill hooks."""
    if not isinstance(group, dict):
        return False
    for h in group.get("hooks", []):
        cmd = h.get("command", "") if isinstance(h, dict) else ""
        if "skills/unifable/hooks" in cmd or "skills/fablize/hooks" in cmd:
            return True
    return False


def merge(path: str) -> None:
    try:
        cfg = json.load(open(path)) if os.path.exists(path) else {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    hooks = cfg.setdefault("hooks", {})

    for event, groups in UNIFABLE.items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            existing = []
        kept = [g for g in existing if not _is_ours(g)]  # preserve non-unifable hooks
        hooks[event] = kept + groups

    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    print("  ✓ hooks.json merged (unifable entries added; other hooks preserved)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: merge_hooks.py <path-to-hooks.json>", file=sys.stderr)
        raise SystemExit(2)
    merge(sys.argv[1])
