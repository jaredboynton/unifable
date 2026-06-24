#!/usr/bin/env python3
"""Install-detected copy for explore-skill trace.sh in Bash whitelist guidance.

Host-agnostic: resolves whether the explore skill is present on disk and builds
user-facing allowlist strings. Enforcement (basename trace.sh) lives in
bash_classify.py and is intentionally broader than this guidance.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path

_EXPLORE_NAME_RE = re.compile(r"(?m)^name:\s*explore\s*$")

_DEFAULT_EXPLORE_ROOTS = (
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
def _resolve_explore_trace_sh_cached(home: str, override: str) -> str | None:
    """Return trace.sh path string or None; keyed by home + env override."""
    roots: tuple[Path, ...]
    if override:
        roots = (Path(override).expanduser(),)
    else:
        home_path = Path(home)
        roots = tuple(home_path / rel for rel in _DEFAULT_EXPLORE_ROOTS)
    for root in roots:
        if not _valid_explore_skill_root(root):
            continue
        return str((root / "scripts" / "trace.sh").resolve())
    return None


def resolve_explore_trace_sh() -> Path | None:
    """Return the explore skill's trace.sh when SKILL.md + script exist."""
    raw = _resolve_explore_trace_sh_cached(
        str(_home()),
        os.environ.get("UNIFABLE_EXPLORE_SKILL_ROOT", "").strip(),
    )
    return Path(raw) if raw else None


def clear_explore_guidance_cache() -> None:
    """Clear resolver cache (tests and setup re-runs)."""
    _resolve_explore_trace_sh_cached.cache_clear()


def explore_trace_list_item() -> str:
    """Comma-prefixed list item for parenthesized allowlists, or empty."""
    path = resolve_explore_trace_sh()
    if path is None:
        return ""
    return f", the explore skill's trace.sh ({_display_path(path)})"


def explore_trace_list_item_md() -> str:
    """Markdown comma-prefixed list item for setup/unifable-block.md."""
    path = resolve_explore_trace_sh()
    if path is None:
        return ""
    return f", the explore skill's `trace.sh` (`{_display_path(path)}`)"


def explore_trace_inline_prefix() -> str:
    """Inline prefix before the next allowlist item, or empty."""
    path = resolve_explore_trace_sh()
    if path is None:
        return ""
    return f"the explore skill's trace.sh ({_display_path(path)}), "


def explore_trace_inline_md() -> str:
    """Markdown inline prefix for setup/unifable-block.md."""
    path = resolve_explore_trace_sh()
    if path is None:
        return ""
    return f"the explore skill's `trace.sh` (`{_display_path(path)}`), "


def groundedness_bash_whitelist_fragment() -> str:
    """Middle clause for judge steering schema descriptions."""
    path = resolve_explore_trace_sh()
    if path is None:
        return ""
    return f"the explore skill's trace.sh ({_display_path(path)}), "


def bash_allowed_summary() -> str:
    """Compact allowlist for PreToolUse block messages and breaker copy."""
    parts = [
        "cd, ls, glob, rg, read-only git, git add/commit/push (no --force)",
    ]
    explore = explore_trace_list_item()
    if explore:
        parts.append(explore.lstrip(", "))
    parts.append("unifusion scripts, unifable spec CLI")
    return ", ".join(parts)


def allowed_research_bash_detail() -> str:
    """Long-form research Bash allowlist for docs and module reference."""
    explore = explore_trace_list_item()
    explore_clause = explore if explore else ""
    return (
        "cd, ls, glob, rg, read-only git (status, log, diff, show, rev-parse, describe, branch, remote, "
        "tag -l, stash list, blame, shortlog, reflog, merge-base, name-rev, config get), "
        "git workflow (add, commit, push without --force), "
        "read-only pipeline sinks (head, tail, wc, sort, uniq) after those"
        f"{explore_clause}, "
        "the unifusion skill scripts unifusion.sh|save_run.sh|summarize_session.sh|resolve_session.sh "
        "(~/.claude/skills/unifusion/scripts/), or the append-only spec CLI "
        "(unifable restate|add-task|set-primary|add-frontier|dispute|retry-task; legacy unifable-spec alias still accepted)"
    )
