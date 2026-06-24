#!/usr/bin/env python3
"""Assert the plugin version is consistent across every managed manifest and was
bumped past the pre-fix baseline (T5 check).

Mirrors the managed set in scripts/bump_version.py: the four plugin dirs
(plugin.json + marketplace.json), setup/setup.sh, and the `just version X.Y.Z`
example in AGENTS.md must all read one semver, and it must differ from the
pre-fix 1.9.24 so a release that forgot to bump fails this check."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PRE_FIX = "1.9.24"
VERSION_FIELD = re.compile(r'"version"\s*:\s*"(\d+\.\d+\.\d+)"')
JUST_VERSION = re.compile(r"\bjust version (\d+\.\d+\.\d+)\b")

FIELD_FILES = [
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".codex-plugin/plugin.json",
    ".codex-plugin/marketplace.json",
    ".devin-plugin/plugin.json",
    ".devin-plugin/marketplace.json",
    ".factory-plugin/plugin.json",
    ".factory-plugin/marketplace.json",
    "setup/setup.sh",
]


def main() -> int:
    canonical = json.loads((REPO / ".claude-plugin/plugin.json").read_text())["version"]
    errors: list[str] = []

    for rel in FIELD_FILES:
        p = REPO / rel
        if not p.exists():
            continue
        for v in VERSION_FIELD.findall(p.read_text()):
            if v != canonical:
                errors.append(f"{rel}: {v} != canonical {canonical}")

    agents = REPO / "AGENTS.md"
    if agents.exists():
        for v in JUST_VERSION.findall(agents.read_text()):
            if v != canonical:
                errors.append(f"AGENTS.md `just version {v}` != canonical {canonical}")

    if canonical == PRE_FIX:
        errors.append(f"version still at pre-fix baseline {PRE_FIX}; it must be bumped")

    if errors:
        print("VERSION INCONSISTENT:")
        for e in errors:
            print(f"  {e}")
        return 1
    print(f"versions consistent at {canonical} across all managed manifests (past {PRE_FIX})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
