#!/usr/bin/env python3
"""Pointer + rehydrate for judge directive/steering file references.

The gpt-realtime judge occasionally truncates long compound filenames when it
re-types them in free prose (research_bash_guidance.py -> ash_guidance.py;
groundedness_facade_api.py -> ness_facade_api.py): it drops leading subword
token(s) of a rare identifier it is reconstructing from context.

To make file references lossless instead of repaired-after-the-fact, the host
hands the judge a numbered FILE INDEX of the paths it already saw in the
transcript; the judge names a file by its index in double brackets ([[n]])
rather than typing the path; the host rehydrates [[n]] back to the exact path.
The judge emits integers, never path strings, so truncation is impossible by
construction. Mirrors the explore skill's READ INDEX / excerpt_index pointer
submit (skills/explore/scripts/lib/rt-rehydrate-submit.mjs).
"""
from __future__ import annotations

import re

# File extensions worth indexing as referenceable paths in judge guidance.
_EXT = (
    "py", "md", "json", "mjs", "js", "ts", "tsx", "jsx", "sh", "toml", "txt",
    "yaml", "yml", "cfg", "ini", "rs", "go", "rb", "java", "c", "h", "cpp",
)
_PATH_RE = re.compile(r"(?<![\w./-])([\w./-]+\.(?:" + "|".join(_EXT) + r"))(?![\w])")
_REF_RE = re.compile(r"\[\[\s*(\d{1,3})\s*\]\]")

# Cap the index so it cannot flood the judge prompt; first-seen order keeps the
# most contextually salient paths (they appear earliest in the rendered tail).
MAX_INDEX_FILES = 50

_INDEX_HEADER = (
    "FILE INDEX -- to name any file below in `directive` or `steering`, write its "
    "number in double brackets (e.g. [[2]]) INSTEAD of typing the path; the host "
    "rehydrates the exact path. Do NOT retype these paths yourself (you truncate long "
    "names). Type a path literally only for a file not listed here."
)


def extract_paths(segment: str) -> list[str]:
    """Distinct file-path tokens in first-seen order from the judge transcript."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for m in _PATH_RE.finditer(segment or ""):
        path = m.group(1)
        if path in seen_set:
            continue
        seen_set.add(path)
        seen.append(path)
        if len(seen) >= MAX_INDEX_FILES:
            break
    return seen


def build_file_index(segment: str) -> tuple[str, list[str]]:
    """Render the numbered FILE INDEX block and the ordered path list it encodes.

    Returns ("", []) when the segment names no files, so the caller appends
    nothing and the judge path is unchanged.
    """
    paths = extract_paths(segment)
    if not paths:
        return "", []
    lines = [_INDEX_HEADER]
    for i, path in enumerate(paths):
        lines.append(f"[{i}] {path}")
    return "\n".join(lines), paths


def rehydrate_file_refs(text: str, paths: list[str]) -> str:
    """Replace [[n]] pointers in judge guidance with paths[n].

    Out-of-range indices are left verbatim (visible, not silently mangled) so a
    bad pointer fails safe rather than resolving to the wrong file. Inputs with
    no pointer are returned unchanged.
    """
    if not text or "[[" not in text:
        return text or ""

    def sub(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(paths):
            return paths[idx]
        return m.group(0)

    return _REF_RE.sub(sub, text)
