#!/usr/bin/env python3
"""Bump the unifable plugin version across every manifest and setup/setup.sh.

This is the single enforcement point for the version-bump convention in
AGENTS.md: the version string lives in four plugin dirs (each a plugin.json plus
a marketplace.json) and in setup/setup.sh's progress.json writer. This sets them
all to one target value in a single pass and refuses to finish if any straggler
of the old version survives in the managed set.

Usage:
    python3 scripts/bump_version.py 1.9.4    # set an explicit version
    python3 scripts/bump_version.py patch     # 1.9.3 -> 1.9.4
    python3 scripts/bump_version.py minor     # 1.9.3 -> 1.10.0
    python3 scripts/bump_version.py major     # 1.9.3 -> 2.0.0

Invoked via `just version <arg>`.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Every file that carries the plugin version. The codex/devin marketplace.json
# files have no version key today; they are listed anyway so the set stays
# complete if one is added later -- the regex simply no-ops on a file without a
# version field.
TARGETS = [
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

CANONICAL = ".claude-plugin/plugin.json"  # source of the current version
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
# Matches a JSON-style version field with a plain semver value, in both .json
# manifests and the "version": "X.Y.Z" literal inside setup/setup.sh's heredoc.
VERSION_FIELD = re.compile(r'("version"\s*:\s*")\d+\.\d+\.\d+(")')


def current_version() -> str:
    data = json.loads((REPO / CANONICAL).read_text())
    v = str(data.get("version", ""))
    if not SEMVER.match(v):
        sys.exit(f"bump_version: canonical {CANONICAL} has no valid semver (got {v!r})")
    return v


def resolve_target(arg: str, old: str) -> str:
    if SEMVER.match(arg):
        return arg
    major, minor, patch = (int(x) for x in old.split("."))
    if arg == "major":
        return f"{major + 1}.0.0"
    if arg == "minor":
        return f"{major}.{minor + 1}.0"
    if arg == "patch":
        return f"{major}.{minor}.{patch + 1}"
    sys.exit(f"bump_version: '{arg}' is not a semver (X.Y.Z) or one of major|minor|patch")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.exit("usage: bump_version.py <X.Y.Z|major|minor|patch>")
    old = current_version()
    new = resolve_target(argv[0], old)

    total = 0
    changed: list[tuple[str, int]] = []
    for rel in TARGETS:
        p = REPO / rel
        if not p.exists():
            continue
        text = p.read_text()
        new_text, n = VERSION_FIELD.subn(rf"\g<1>{new}\g<2>", text)
        if n and new_text != text:
            p.write_text(new_text)
            changed.append((rel, n))
            total += n

    print(f"bump_version: {old} -> {new}")
    for rel, n in changed:
        print(f"  {rel}: {n} field(s)")

    if old == new:
        print("bump_version: already at target; nothing to change")
        return 0
    if total == 0:
        sys.exit(f"bump_version: no version fields matched; nothing changed (old={old})")

    # Hard check: no managed target may still pin the old version.
    for rel in TARGETS:
        p = REPO / rel
        if not p.exists():
            continue
        if re.search(rf'"version"\s*:\s*"{re.escape(old)}"', p.read_text()):
            sys.exit(f"bump_version: {rel} still pins old version {old}")

    # Soft warn: the old version lingering elsewhere (README, docs) that this tool
    # does not manage -- surfaced, never silently left.
    strays: list[str] = []
    for f in REPO.rglob("*"):
        rel = str(f.relative_to(REPO))
        if not f.is_file() or rel in TARGETS:
            continue
        # Skip VCS, deps, build caches, and the gate's own per-task state under
        # .unifable/ (specs/ledgers echo run output and are not shippable source).
        if any(seg in rel for seg in (".git/", "node_modules/", "__pycache__/", ".unifable/")):
            continue
        if f.suffix not in (".json", ".sh", ".md", ".toml"):
            continue
        try:
            if re.search(rf"\b{re.escape(old)}\b", f.read_text()):
                strays.append(rel)
        except (OSError, UnicodeDecodeError):
            pass
    if strays:
        print(f"  warning: old version {old} also appears in (not managed here):")
        for s in strays:
            print(f"    {s}")

    print(f"bump_version: all {total} version field(s) set to {new}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
