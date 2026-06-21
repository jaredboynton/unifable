#!/usr/bin/env python3
"""Bump the unifable plugin version across every manifest, setup/setup.sh, and
the `just version` example in AGENTS.md.

This is the single enforcement point for the version-bump convention in
AGENTS.md: the version string lives in four plugin dirs (each a plugin.json plus
a marketplace.json), in setup/setup.sh's progress.json writer, and in AGENTS.md's
concrete `just version X.Y.Z` example. This sets them all to one target value in
a single pass and refuses to finish if any managed pattern still reads the old
version.

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

CANONICAL = ".claude-plugin/plugin.json"  # source of the current version
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# Each pattern captures the semver in group(2) so the post-bump check is uniform.
# VERSION_FIELD: a JSON "version": "X.Y.Z" field -- also matches the literal
# inside setup/setup.sh's heredoc. JUST_VERSION: the concrete `just version X.Y.Z`
# example in AGENTS.md; the `just version <X.Y.Z>` angle-bracket form and the
# patch|minor|major forms carry no semver, so they are left untouched.
VERSION_FIELD = re.compile(r'("version"\s*:\s*")(\d+\.\d+\.\d+)(")')
JUST_VERSION = re.compile(r"(\bjust version )(\d+\.\d+\.\d+)\b")

# (path, pattern, replacement template). {new} is filled per run; the template
# uses the pattern's own capture groups. The codex/devin marketplace.json files
# have no version key today but stay listed so the set is complete if one is
# added later -- the pattern simply no-ops on a file with no match.
TARGETS: list[tuple[str, "re.Pattern[str]", str]] = [
    (".claude-plugin/plugin.json",       VERSION_FIELD, r"\g<1>{new}\g<3>"),
    (".claude-plugin/marketplace.json",  VERSION_FIELD, r"\g<1>{new}\g<3>"),
    (".codex-plugin/plugin.json",        VERSION_FIELD, r"\g<1>{new}\g<3>"),
    (".codex-plugin/marketplace.json",   VERSION_FIELD, r"\g<1>{new}\g<3>"),
    (".devin-plugin/plugin.json",        VERSION_FIELD, r"\g<1>{new}\g<3>"),
    (".devin-plugin/marketplace.json",   VERSION_FIELD, r"\g<1>{new}\g<3>"),
    (".factory-plugin/plugin.json",      VERSION_FIELD, r"\g<1>{new}\g<3>"),
    (".factory-plugin/marketplace.json", VERSION_FIELD, r"\g<1>{new}\g<3>"),
    ("setup/setup.sh",                   VERSION_FIELD, r"\g<1>{new}\g<3>"),
    ("AGENTS.md",                        JUST_VERSION,  r"\g<1>{new}"),
]
MANAGED = {rel for rel, _, _ in TARGETS}


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
    for rel, pat, repl in TARGETS:
        p = REPO / rel
        if not p.exists():
            continue
        text = p.read_text()
        new_text, n = pat.subn(repl.format(new=new), text)
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

    # Hard check: every version a managed pattern captures now reads `new`.
    for rel, pat, _ in TARGETS:
        p = REPO / rel
        if not p.exists():
            continue
        for m in pat.finditer(p.read_text()):
            if m.group(2) != new:
                sys.exit(f"bump_version: {rel} still has version {m.group(2)} (expected {new})")

    # Soft warn: the old version lingering elsewhere (README, docs) that this tool
    # does not manage -- surfaced, never silently left.
    strays: list[str] = []
    for f in REPO.rglob("*"):
        rel = str(f.relative_to(REPO))
        if not f.is_file() or rel in MANAGED:
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
