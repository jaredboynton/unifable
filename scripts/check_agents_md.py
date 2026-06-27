#!/usr/bin/env python3
"""Validate that every AGENTS.md stays consistent with the code it documents.

Two checks, run over all AGENTS.md files (root + nested), failing nonzero on
the first broken reference so docs cannot drift from the tree:

1. Relative markdown links `[text](target)` resolve to an existing file/dir
   (anchors `#...` stripped). External `http(s)`/`mailto:` links are ignored.
2. Every `just <recipe>` mentioned inside a code span (inline backticks or a
   fenced block) has a matching recipe in the justfile, so documented commands
   stay runnable. `just` appearing in prose is ignored.

A markdown-link target passes if it resolves relative to the referencing file's
own directory OR relative to the repo root, matching how the docs cross-link.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JUSTFILE = ROOT / "justfile"

SKIP_DIRS = {".venv", ".unifable", ".omc", ".omx", ".git", "node_modules"}

MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
JUST_RE = re.compile(r"\bjust\s+([a-z][a-z0-9-]*)\b")
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
RECIPE_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_-]*)\s*(?:[A-Za-z0-9_ ]*)?:")

# `just` references that are recipe arguments / examples, not recipe names.
JUST_KEYWORDS = {"patch", "minor", "major"}


def agents_files() -> list[Path]:
    out: list[Path] = []
    for path in ROOT.rglob("AGENTS.md"):
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        out.append(path)
    return sorted(out)


def justfile_recipes() -> set[str]:
    recipes: set[str] = set()
    if not JUSTFILE.exists():
        return recipes
    for line in JUSTFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith((" ", "\t", "#", "@")) or ":" not in line:
            continue
        m = RECIPE_RE.match(line)
        if m and not m.group(1).startswith("_"):
            recipes.add(m.group(1))
    return recipes


def resolves(candidate: str, doc: Path) -> bool:
    target = candidate.split("#", 1)[0].strip()
    if not target:
        return True  # pure in-page anchor
    rel = Path(target)
    if rel.is_absolute():
        return rel.exists()
    return (doc.parent / rel).exists() or (ROOT / rel).exists()


def code_spans(text: str) -> str:
    fences = " ".join(FENCE_RE.findall(text))
    inline = " ".join(INLINE_CODE_RE.findall(text))
    return f"{fences} {inline}"


def main() -> int:
    recipes = justfile_recipes()
    errors: list[str] = []
    files = agents_files()

    for doc in files:
        rel_doc = doc.relative_to(ROOT).as_posix()
        text = doc.read_text(encoding="utf-8")

        for link in MD_LINK_RE.findall(text):
            if re.match(r"^(?:https?:|mailto:|#)", link.strip()):
                continue
            if not resolves(link, doc):
                errors.append(f"{rel_doc}: broken link -> {link}")

        for recipe in JUST_RE.findall(code_spans(text)):
            if recipe in JUST_KEYWORDS:
                continue
            if recipe not in recipes:
                errors.append(f"{rel_doc}: `just {recipe}` has no matching recipe in justfile")

    if errors:
        print("AGENTS.md validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print(f"AGENTS.md validation passed across {len(files)} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
