#!/usr/bin/env python3
"""Cross-check that a spec's citations are REAL, not fabricated.

validate_spec (spec.py) checks citation FORMAT (path:line, url, why, no fake
markers). This module checks their TRUTH against what the session actually did:
- repo_context 'path:line'   -> that file was actually Read/grepped this session
- prior_art 'url'         -> that URL was actually fetched (WebFetch/curl) this session
- acceptance 'check' cmd  -> that command was actually executed (Bash) this session

Source of truth is the ledger activity log (read_paths / fetched_urls /
ran_commands), recorded by gate_post_tool.py on every tool call -- host-agnostic
and available at BOTH the pre-edit gate and Stop. At Stop the session transcript
(transcript_path, recursing sub-agent transcripts) is UNION'd in to corroborate
and to catch reads done by sub-agents.

Matching is hardened against the obvious bypasses (see the design review):
- paths: resolved to absolute; multi-segment suffix match; NEVER bare-basename
  (so two files named utils.py don't credit each other).
- urls: host+path compared via urllib (http/https-equivalent, query/fragment and
  trailing slash ignored); no exploitable startswith.
- commands: shlex token-PREFIX per shell segment (so `cd x && pytest tests/`
  counts, but `echo pytest tests/` does not, and a broad cite can't be satisfied
  by a narrower run).

Disabled only with UNIFABLE_VERIFY_CITATIONS=0 (escape hatch).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from parse_tool_result import (
    _SHELL_OPERATORS,
    _tokens,
    fetched_url_targets,
    ran_command,
    read_targets,
)
from spec import repo_context_parts, prior_art_parts, repo_context_of

try:
    from urllib.parse import urlsplit
except ImportError:  # pragma: no cover
    urlsplit = None  # type: ignore

ACTIVITY_KEYS = ("read_paths", "fetched_urls", "ran_commands")
_SHELL_SPLIT_RE = re.compile(r"&&|\|\||;|\|")
_LINE_SUFFIX_RE = re.compile(r"^(?P<path>.*):\d+(?:-\d+)?$")


def enabled() -> bool:
    return os.environ.get("UNIFABLE_VERIFY_CITATIONS", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def empty_activity() -> dict[str, list[str]]:
    return {"read_paths": [], "fetched_urls": [], "ran_commands": []}


def activity_from_ledger(ledger: dict[str, Any]) -> dict[str, list[str]]:
    out = empty_activity()
    for key in ACTIVITY_KEYS:
        value = ledger.get(key)
        if isinstance(value, list):
            out[key] = [str(x) for x in value if x]
    return out


def merge_activity(*activities: dict[str, list[str]]) -> dict[str, list[str]]:
    out = empty_activity()
    for key in ACTIVITY_KEYS:
        seen: set[str] = set()
        merged: list[str] = []
        for act in activities:
            for value in act.get(key, []) or []:
                if value and value not in seen:
                    seen.add(value)
                    merged.append(value)
        out[key] = merged
    return out


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _abs(path: str, cwd: str) -> str:
    try:
        p = Path(path)
        if not p.is_absolute():
            p = Path(cwd) / p
        return str(p.resolve())
    except (OSError, ValueError):
        return path


def _cite_path(cite: str) -> str:
    """Strip a ':line' / ':start-end' suffix from a path:line citation."""
    m = _LINE_SUFFIX_RE.match(cite.strip())
    return m.group("path") if m else cite.strip()


def path_was_read(cite: str, read_paths: list[str], cwd: str) -> bool:
    raw = _cite_path(cite)
    if not raw:
        return False
    target = _abs(raw, cwd)
    reads = {_abs(r, cwd) for r in read_paths}
    if target in reads:
        return True
    # Multi-segment suffix match tolerates a different cwd/prefix between the
    # recorded read and the cite (e.g. read '/repo/scripts/gate/spec.py' satisfies
    # cite 'scripts/gate/spec.py'). A bare basename ('spec.py') is NOT accepted --
    # it would let any same-named file credit the citation.
    rel = raw.lstrip("./")
    if "/" in rel:
        needle = "/" + rel
        if any(r.replace(os.sep, "/").endswith(needle) for r in reads):
            return True
    return False


def _norm_url(url: str) -> tuple[str, str]:
    """(host, path) lowercased host, trailing-slash-stripped path; '' on garbage.
    Scheme, port, query and fragment are intentionally dropped."""
    if urlsplit is None:
        return "", url.strip()
    try:
        s = urlsplit(url.strip())
        host = (s.hostname or "").lower()
        path = s.path.rstrip("/")
        return host, path
    except ValueError:
        return "", url.strip()


def url_was_fetched(cite: str, fetched_urls: list[str]) -> bool:
    c_host, c_path = _norm_url(cite)
    if not c_host:
        return False
    for f in fetched_urls:
        f_host, f_path = _norm_url(f)
        if f_host == c_host and f_path == c_path:
            return True
    return False


def command_was_run(check: str, ran_commands: list[str]) -> bool:
    want = _tokens(check)
    if not want:
        return False
    n = len(want)
    for cmd in ran_commands:
        for segment in _SHELL_SPLIT_RE.split(cmd):
            got = [t for t in _tokens(segment) if t not in _SHELL_OPERATORS]
            if got[:n] == want:
                return True
    return False


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def verify_citations(
    spec: dict[str, Any],
    activity: dict[str, list[str]],
    cwd: str,
    *,
    require_commands: bool,
) -> list[str]:
    """Return reasons each citation is NOT backed by real activity (empty = ok).

    require_commands: at pre-edit time the work is not done yet, so acceptance
    check commands are not required to have run; at Stop they are."""
    reasons: list[str] = []
    read_paths = activity.get("read_paths", []) or []
    fetched = activity.get("fetched_urls", []) or []
    ran = activity.get("ran_commands", []) or []

    for i, item in enumerate(repo_context_of(spec)):
        cite, _why = repo_context_parts(item)
        if cite and not path_was_read(cite, read_paths, cwd):
            reasons.append(
                f"repo_context[{i}]: {cite!r} (never read this session)"
            )

    for i, item in enumerate(spec.get("prior_art") or []):
        cite, _why = prior_art_parts(item)
        if cite and not url_was_fetched(cite, fetched):
            reasons.append(
                f"prior_art[{i}]: {cite!r} (never fetched this session)"
            )

    if require_commands:
        for i, ac in enumerate(spec.get("acceptance_criteria") or []):
            check = ac.get("check") if isinstance(ac, dict) else ""
            if check and not command_was_run(str(check), ran):
                reasons.append(
                    f"acceptance_criteria[{i}].check {str(check)!r} (never run this session)"
                )

    return reasons


def format_citation_verify_message(reasons: list[str]) -> str:
    """One headline, compact per-cite lines, shared footnotes (no repeated boilerplate)."""
    items = [str(r).strip() for r in (reasons or []) if str(r).strip()]
    if not items:
        return ""
    lines = ["spec citations are not backed by real activity this session:"]
    lines.extend(f"  {item}" for item in items)
    footnotes: list[str] = []
    if any(r.startswith("repo_context[") for r in items):
        footnotes.append(
            "Read each cited file (Read/grep) before citing it "
            "(the gate verifies repo_context against actual tool activity)."
        )
    if any(r.startswith("prior_art[") for r in items):
        footnotes.append(
            "Fetch each URL (WebFetch or curl) before citing it as prior art."
        )
    if any(r.startswith("acceptance_criteria[") for r in items):
        footnotes.append(
            "Run each acceptance check command before citing its output."
        )
    if footnotes:
        lines.append("")
        lines.extend(footnotes)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Transcript scan (Stop-only corroboration; recurses sub-agent transcripts)
# ---------------------------------------------------------------------------

def _scan_blocks(content: Any, act: dict[str, list[str]], cwd: str) -> None:
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        pseudo = {"tool_name": block.get("name"), "tool_input": block.get("input") or {}}
        for p in read_targets(pseudo):
            act["read_paths"].append(_abs(p, cwd))
        act["fetched_urls"].extend(fetched_url_targets(pseudo))
        rc = ran_command(pseudo)
        if rc:
            act["ran_commands"].append(rc)


def _scan_file(path: Path, act: dict[str, list[str]]) -> None:
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                msg = entry.get("message")
                if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                    cwd = str(entry.get("cwd") or os.getcwd())
                    _scan_blocks(msg["content"], act, cwd)
    except OSError:
        return


def scan_transcript(transcript_path: str | None) -> dict[str, list[str]]:
    """Activity replayed from a session transcript JSONL, recursing the session's
    sub-agent transcripts at `<dir>/<uuid>/subagents/*.jsonl`. Empty on any miss."""
    act = empty_activity()
    if not transcript_path:
        return act
    p = Path(transcript_path)
    targets: list[Path] = []
    if p.is_file():
        targets.append(p)
    subdir = p.parent / p.stem / "subagents"
    if subdir.is_dir():
        targets.extend(sorted(subdir.rglob("*.jsonl")))
    for tp in targets:
        _scan_file(tp, act)
    # dedup
    return merge_activity(act)


# ---------------------------------------------------------------------------
# Auto-cite from session activity (hook-driven; replaces agent `cite` CLI)
# ---------------------------------------------------------------------------

_READ_WHY = "read this session"
_FETCH_WHY = "fetched this session"


def _path_to_cite(abs_path: str, cwd: str) -> str:
    """Convert an absolute read path to a repo-relative path:line cite."""
    try:
        p = Path(abs_path).resolve()
        root = Path(cwd).resolve()
        rel = p.relative_to(root)
        return f"{rel.as_posix()}:1"
    except (ValueError, OSError):
        name = Path(abs_path).name
        if "/" in str(abs_path).replace(os.sep, "/"):
            parts = str(abs_path).replace(os.sep, "/").split("/")
            if len(parts) >= 2:
                return f"{parts[-2]}/{parts[-1]}:1"
        return f"{name}:1" if name else ""


def _existing_repo_cites(spec: dict[str, Any]) -> set[str]:
    cites: set[str] = set()
    for item in repo_context_of(spec):
        cite, _ = repo_context_parts(item)
        if cite:
            cites.add(cite.strip())
            cites.add(_cite_path(cite.strip()))
    return cites


def _existing_prior_urls(spec: dict[str, Any]) -> set[str]:
    urls: set[str] = set()
    for item in spec.get("prior_art") or []:
        cite, _ = prior_art_parts(item)
        if cite:
            host, path = _norm_url(cite.strip())
            if host:
                urls.add(f"{host}{path}")
    return urls


def sync_citations_from_activity(
    spec: dict[str, Any],
    activity: dict[str, list[str]],
    cwd: str,
    *,
    added_sink: dict[str, list[str]] | None = None,
) -> bool:
    """Append repo_context / prior_art from ledger activity. Returns True if mutated.

    When ``added_sink`` is provided it is filled with the cites this call
    appended: ``{"repo_context": [path:line, ...], "prior_art": [url, ...]}`` so a
    hook can name what it auto-synced. Existing callers pass nothing and are
    unaffected."""
    if not isinstance(spec, dict):
        return False
    changed = False
    spec.setdefault("repo_context", [])
    spec.setdefault("prior_art", [])
    # Drop scaffold placeholders so auto-cites are substantive.
    spec["repo_context"] = [
        item for item in spec["repo_context"]
        if isinstance(item, dict) and str(item.get("cite") or "").strip()
    ]
    spec["prior_art"] = [
        item for item in spec["prior_art"]
        if isinstance(item, dict) and str(item.get("cite") or "").strip()
    ]
    seen_paths = _existing_repo_cites(spec)
    read_paths = activity.get("read_paths", []) or []

    for raw in read_paths:
        cite = _path_to_cite(str(raw), cwd)
        if not cite or cite in seen_paths:
            continue
        if not path_was_read(cite, read_paths, cwd):
            continue
        spec["repo_context"].append({"cite": cite, "why": _READ_WHY})
        seen_paths.add(cite)
        if added_sink is not None:
            added_sink.setdefault("repo_context", []).append(cite)
        changed = True

    seen_urls = _existing_prior_urls(spec)
    for url in activity.get("fetched_urls", []) or []:
        u = str(url).strip()
        if not u:
            continue
        host, path = _norm_url(u)
        key = f"{host}{path}" if host else u
        if key in seen_urls:
            continue
        if not url_was_fetched(u, activity.get("fetched_urls", []) or []):
            continue
        spec["prior_art"].append({"cite": u, "why": _FETCH_WHY})
        seen_urls.add(key)
        if added_sink is not None:
            added_sink.setdefault("prior_art", []).append(u)
        changed = True

    return changed
