#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = Path("docs/testing-optimization.md")
SEARCH_DIRS = ("tests", "scripts", "hooks", "benchmark")
SUFFIXES = {".py", ".md"}
SCAN_RE = re.compile("|".join(("sleep\\(", "time\\.sleep", "wa" + "it", "time" + "out")))
SLEEP_RE = re.compile("|".join(("sleep\\(", "time\\.sleep")))

COVERED = {
    "benchmark/bench.py",
    "docs/testing-optimization.md",
    "hooks/gate_stop.py",
    "hooks/test_after_edit.py",
    "scripts/audit_waits.py",
    "scripts/gate/breaker_state.py",
    "scripts/gate/cli_install.py",
    "scripts/gate/codex_judge.py",
    "scripts/gate/context_block.py",
    "scripts/gate/grade_override.py",
    "scripts/gate/groundedness.py",
    "scripts/gate/judge_client.py",
    "scripts/gate/judge_daemon.py",
    "scripts/gate/judge_transport.py",
    "scripts/gate/runtime_sync.py",
    "scripts/gate/spec.py",
    "scripts/generate_docs.py",
    "scripts/shadow/outcome_collect.py",
    "tests/test_auto_validate_stop.py",
    "tests/test_completion_handoff.py",
    "tests/test_grade_adjudicate_hook.py",
    "tests/test_judge_coalesce.py",
    "tests/test_judge_message_cap.py",
    "tests/test_judge_runaway.py",
    "tests/test_loop_release.py",
    "tests/test_mcp_evidence.py",
    "tests/test_runtime_sync.py",
    "tests/test_spec_state_notifications.py",
    "tests/test_stop_codex_json.py",
    "tests/test_stop_timeout_budget.py",
    "tests/test_supersession.py",
    "tests/test_test_after_edit.py",
}


def files_to_scan() -> list[Path]:
    out = [ROOT / DOC]
    for dirname in SEARCH_DIRS:
        for path in (ROOT / dirname).rglob("*"):
            if path.is_file() and path.suffix in SUFFIXES:
                out.append(path)
    return sorted(set(out))


def matching_files(pattern: re.Pattern[str]) -> set[str]:
    matches: set[str] = set()
    for path in files_to_scan():
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if pattern.search(text):
            matches.add(rel)
    return matches


def main() -> int:
    matches = matching_files(SCAN_RE)
    doc_text = (ROOT / DOC).read_text(encoding="utf-8")
    missing = sorted(matches - COVERED)
    stale = sorted(COVERED - matches)
    undocumented = sorted(rel for rel in matches if rel != DOC.as_posix() and f"`{rel}`" not in doc_text)
    test_sleeps = sorted(rel for rel in matching_files(SLEEP_RE) if rel.startswith("tests/"))

    if missing or stale or undocumented or test_sleeps:
        if missing:
            print("uncovered files:", ", ".join(missing), file=sys.stderr)
        if stale:
            print("stale coverage:", ", ".join(stale), file=sys.stderr)
        if undocumented:
            print("missing doc entries:", ", ".join(undocumented), file=sys.stderr)
        if test_sleeps:
            print("test sleep calls remain:", ", ".join(test_sleeps), file=sys.stderr)
        return 1

    print(f"latency audit covered {len(matches)} grep-matched file(s)")
    print("test sleep calls: 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
