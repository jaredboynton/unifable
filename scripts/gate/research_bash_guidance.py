#!/usr/bin/env python3
"""Install-detected copy for explore-skill scripts in Bash whitelist guidance.

Host-agnostic: resolves whether the explore skill is present on disk and builds
user-facing allowlist strings. Enforcement (basename trace.sh / websearch.sh)
lives in bash_classify.py and is intentionally broader than this guidance.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path

_EXPLORE_NAME_RE = re.compile(r"(?m)^name:\s*explore\s*$")

EXPLORE_SCRIPT_BASENAMES = ("trace.sh", "websearch.sh", "search.sh")

# The stable central runtime (~/.unifable/current/skills/explore) is preferred:
# runtime_sync seeds it from the newest plugin version every SessionStart, so it
# resolves regardless of which CLI or plugin cache is active and survives deletion
# of the legacy hand-maintained host copies below. The explore skill's scripts/
# holds all three entrypoints (trace.sh, search.sh, websearch.sh); the sibling
# explore-websearch skill is a thin delegating entrypoint over the same code.
_DEFAULT_EXPLORE_ROOTS = (
    ".unifable/current/skills/explore",
    ".agents/skills/explore",
    ".claude/skills/explore",
    ".cursor/skills/explore",
)


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _display_path(path: Path) -> str:
    """Shorten absolute paths under $HOME for compact hook copy."""
    resolved = path.resolve()
    home = _home().resolve()
    try:
        rel = resolved.relative_to(home)
    except ValueError:
        return str(resolved)
    return f"~/{rel.as_posix()}"


def _valid_explore_skill_root(root: Path) -> bool:
    skill_md = root / "SKILL.md"
    trace_sh = root / "scripts" / "trace.sh"
    if not skill_md.is_file() or not trace_sh.is_file():
        return False
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return False
    return bool(_EXPLORE_NAME_RE.search(text))


@functools.lru_cache(maxsize=8)
def _resolve_explore_skill_root_cached(home: str, override: str) -> str | None:
    """Return explore skill root path string or None; keyed by home + env override."""
    roots: tuple[Path, ...]
    if override:
        roots = (Path(override).expanduser(),)
    else:
        home_path = Path(home)
        roots = tuple(home_path / rel for rel in _DEFAULT_EXPLORE_ROOTS)
    for root in roots:
        if _valid_explore_skill_root(root):
            return str(root.resolve())
    return None


def _resolve_explore_skill_root() -> Path | None:
    raw = _resolve_explore_skill_root_cached(
        str(_home()),
        os.environ.get("UNIFABLE_EXPLORE_SKILL_ROOT", "").strip(),
    )
    return Path(raw) if raw else None


def _installed_explore_scripts() -> list[tuple[str, Path]]:
    """Return installed explore scripts as (basename, path) pairs in stable order."""
    root = _resolve_explore_skill_root()
    if root is None:
        return []
    out: list[tuple[str, Path]] = []
    for name in EXPLORE_SCRIPT_BASENAMES:
        script = root / "scripts" / name
        if script.is_file():
            out.append((name, script.resolve()))
    return out


def resolve_explore_trace_sh() -> Path | None:
    """Return the explore skill's trace.sh when SKILL.md + script exist."""
    for name, path in _installed_explore_scripts():
        if name == "trace.sh":
            return path
    return None


def resolve_explore_websearch_sh() -> Path | None:
    """Return the explore skill's websearch.sh when installed alongside trace.sh."""
    for name, path in _installed_explore_scripts():
        if name == "websearch.sh":
            return path
    return None


def resolve_explore_search_sh() -> Path | None:
    """Return the explore skill's search.sh (fast read-only code search) when
    installed alongside trace.sh. Used by the groundedness breaker to self-resolve
    find/read-checkable claims before arming."""
    for name, path in _installed_explore_scripts():
        if name == "search.sh":
            return path
    return None


def clear_explore_guidance_cache() -> None:
    """Clear resolver cache (tests and setup re-runs)."""
    _resolve_explore_skill_root_cached.cache_clear()


def _explore_scripts_clause(*, markdown: bool) -> str:
    scripts = _installed_explore_scripts()
    if not scripts:
        return ""
    if markdown:
        parts = [f"`{name}` (`{_display_path(path)}`)" for name, path in scripts]
    else:
        parts = [f"{name} ({_display_path(path)})" for name, path in scripts]
    joined = " and ".join(parts)
    return f"the explore skill's {joined}"


def explore_trace_list_item() -> str:
    """Comma-prefixed list item for parenthesized allowlists, or empty."""
    clause = _explore_scripts_clause(markdown=False)
    if not clause:
        return ""
    return f", {clause}"


def explore_trace_list_item_md() -> str:
    """Markdown comma-prefixed list item for context_block.py."""
    clause = _explore_scripts_clause(markdown=True)
    if not clause:
        return ""
    return f", {clause}"


def explore_trace_inline_prefix() -> str:
    """Inline prefix before the next allowlist item, or empty."""
    clause = _explore_scripts_clause(markdown=False)
    if not clause:
        return ""
    return f"{clause}, "


def explore_trace_inline_md() -> str:
    """Markdown inline prefix for context_block.py."""
    clause = _explore_scripts_clause(markdown=True)
    if not clause:
        return ""
    return f"{clause}, "


def groundedness_bash_whitelist_fragment() -> str:
    """Middle clause for judge steering schema descriptions."""
    return explore_trace_inline_prefix()


def explore_trace_compact_item() -> str:
    """Short explore clause for size-budget block messages, or empty."""
    scripts = _installed_explore_scripts()
    if not scripts:
        return ""
    names = "/".join(name for name, _path in scripts)
    return f", explore {names}"


def bash_allowed_summary() -> str:
    """Compact allowlist for PreToolUse block messages and breaker copy."""
    parts = [
        "cd, ls, glob, rg, grep, echo (sink pipes only), ast-grep/sg, head, tail, wc, sort, uniq, "
        "read-only git, git add/commit/push (no --force), read-only python/python3 -c",
    ]
    explore = explore_trace_compact_item()
    if explore:
        parts.append(explore.lstrip(", "))
    parts.append("unifusion scripts, unifable spec CLI")
    return ", ".join(parts)


def allowed_research_bash_detail() -> str:
    """Long-form research Bash allowlist for docs and module reference."""
    explore = explore_trace_list_item()
    explore_clause = explore if explore else ""
    return (
        "cd, ls, glob, rg, grep/egrep/fgrep, echo (read-only pipeline sinks only), "
        "ast-grep/sg (scan/run/test; no --update/--rewrite), "
        "read-only file inspection (head, tail, wc, sort, uniq), "
        "read-only python/python3 -c inspection (no writes, process spawn, or network), "
        "read-only git (status, log, diff, show, rev-parse, describe, branch, remote, "
        "tag -l, stash list, blame, shortlog, reflog show, merge-base, name-rev, "
        "ls-remote, ls-files, ls-tree, cat-file, for-each-ref, show-ref, rev-list, "
        "grep, check-ignore, check-attr, verify-commit, verify-tag, help, archive, "
        "count-objects, merge-tree, config get), "
        "git workflow (add, commit, push without --force)"
        f"{explore_clause}, "
        "the unifusion skill scripts unifusion.sh|save_run.sh|summarize_session.sh|resolve_session.sh "
        "(~/.claude/skills/unifusion/scripts/), or the append-only spec CLI "
        "(unifable restate|add-task|set-primary|add-frontier; legacy unifable-spec alias still accepted)"
    )
