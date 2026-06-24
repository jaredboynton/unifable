#!/usr/bin/env python3
"""Minimal analyzer for session env probe outputs.

Usage:
  python3 scripts/measure_session_env.py /tmp/probe-collection/runs.txt

Parses collected Bash tool results containing:
  UNIFABLE_SESSION_RESOLVED=... SOURCE=...
and optional ---ENV--- followed by grep lines.

Computes:
- % present (env var seen)
- % match to resolved (when both present)
- per-host (claude/codex/cursor) breakdown if inferable from SOURCE or env
- absent / none cases
- flags cases where no session env was available (SOURCE=none)

Intended for post-collection analysis after running the probe documented in AGENTS.md.
Does not drive hosts; feed it logs from real sessions.

Exit 0 always; prints report.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

UNIFABLE_RE = re.compile(r"UNIFABLE_SESSION_RESOLVED=([^\s]*) SOURCE=([^\s]+)")
ENV_RE = re.compile(r"^(CLAUDE_CODE_SESSION_ID|CODEX_THREAD_ID|CURSOR_CONVERSATION_ID|CURSOR_SESSION_ID)=(.+)$")


def parse_runs(text: str) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    in_env = False
    for line in text.splitlines():
        m = UNIFABLE_RE.search(line)
        if m:
            if current:
                runs.append(current)
            current = {
                "resolved": m.group(1) or None,
                "source": m.group(2),
                "env_vars": {},
                "raw": [line],
            }
            in_env = False
            continue
        if current:
            current["raw"].append(line)
        if "---ENV---" in line:
            in_env = True
            continue
        if in_env and current:
            em = ENV_RE.match(line.strip())
            if em:
                current["env_vars"][em.group(1)] = em.group(2)
    if current:
        runs.append(current)
    return runs


def infer_host(source: str, envs: dict[str, str]) -> str:
    if "CLAUDE" in source or any("CLAUDE" in k for k in envs):
        return "claude"
    if "CODEX" in source or any("CODEX" in k for k in envs):
        return "codex"
    if "CURSOR" in source or any("CURSOR" in k for k in envs):
        return "cursor"
    return "unknown"


def analyze(runs: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(runs)
    present = 0
    match = 0
    absent = 0
    by_host: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "present": 0, "match": 0, "absent": 0})
    notes: list[str] = []

    for r in runs:
        res = r["resolved"]
        src = r["source"]
        envs = r["env_vars"]
        host = infer_host(src, envs)
        by_host[host]["total"] += 1

        if res:
            present += 1
            by_host[host]["present"] += 1
            # does any env var value match the resolved?
            matched = any(v == res for v in envs.values())
            if matched:
                match += 1
                by_host[host]["match"] += 1
            else:
                notes.append(f"resolved={res} but no matching env value (source={src}, envs={envs})")
        else:
            absent += 1
            by_host[host]["absent"] += 1
            notes.append(f"no resolved id (source={src}); session env absent in shell")

    pct = lambda n, d: (100.0 * n / d) if d else 0.0
    summary = {
        "total_runs": total,
        "env_present": present,
        "env_present_pct": pct(present, total),
        "resolved_and_env_match": match,
        "match_pct_of_present": pct(match, present) if present else 0,
        "absent_or_none": absent,
        "absent_pct": pct(absent, total),
        "by_host": {h: {k: v for k, v in d.items()} | {"present_pct": pct(d["present"], d["total"])} for h, d in by_host.items()},
        "notes": notes[:20],  # cap
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile", nargs="?", default="-", help="path to collected runs log, or - for stdin")
    args = ap.parse_args(argv)

    if args.logfile == "-" or args.logfile is None:
        text = sys.stdin.read()
    else:
        text = Path(args.logfile).read_text(encoding="utf-8", errors="replace")

    runs = parse_runs(text)
    if not runs:
        print("No runs parsed. Feed output containing UNIFABLE_SESSION_RESOLVED lines.")
        return 0

    s = analyze(runs)
    print("Session env probe analysis")
    print("==========================")
    print(f"total probe runs: {s['total_runs']}")
    print(f"env present: {s['env_present']} ({s['env_present_pct']:.1f}%)")
    print(
        f"  of which resolved matched an env value: {s['resolved_and_env_match']} ({s['match_pct_of_present']:.1f}% of present)"
    )
    print(f"absent/none (no session env in shell): {s['absent_or_none']} ({s['absent_pct']:.1f}%)")
    print()
    print("Per-host:")
    for h, d in sorted(s["by_host"].items()):
        print(
            f"  {h}: total={d['total']} present={d['present']} ({d.get('present_pct', 0):.1f}%) match={d.get('match', 0)} absent={d.get('absent', 0)}"
        )
    print()
    if s["notes"]:
        print("Notable observations (first 20):")
        for n in s["notes"]:
            print(f"  - {n}")
    print()
    print("Interpretation notes:")
    print("  - High present+match across cd/subdir/resume supports env-only binding for that host.")
    print("  - Frequent 'absent' means shells did not receive the session env; fix host injection.")
    print("  - Compare UNIFABLE_SESSION_RESOLVED to the conversation id from hook payload/logs.")
    print("  - Do not average hosts; report Claude Code / Codex / Cursor separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
