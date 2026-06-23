#!/usr/bin/env python3
"""Small JSON ledger for the unifable observation gate.

Ported from fable-ish (gate-comparison experiment, 2026-06-14). Tracks task
routing (active_task, grade), citation-verification activity (read_paths,
fetched_urls, ran_commands), and observation state (changes, verification,
failures). Groundedness breaker state lives in scripts/gate/breaker_state.py.
Ledger state lives under ~/.unifable/ledgers/ (override with UNIFABLE_DATA).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:  # bare import when scripts/gate is on sys.path (hooks + tests); package import otherwise
    from atomicio import write_text_atomic
except ImportError:  # pragma: no cover
    from scripts.gate.atomicio import write_text_atomic


DEFAULT_LEDGER: dict[str, Any] = {
    "task_mode": "quick",
    "grade": "",
    # Active spec key (prompt hash) pinned by gate_prompt.py, locked until its
    # tasks all validate. Must live in DEFAULT_LEDGER so load_ledger preserves it
    # across turns (load rebuilds from these keys; unknown keys are dropped).
    "active_task": None,
    "risk_flags": [],
    "changed_files_seen": False,
    "change_kinds": [],
    "verification_commands": [],
    "verification_results": [],
    "failures": [],
    "warning_count": 0,
    "warnings": [],
    "stop_blocks": 0,
    "goal_stop_blocks": 0,
    # Consecutive Stop blocks caused by the completion breaker (every task not yet
    # validated). Drives the advisory-hint loop in gate_stop.py: once the agent has
    # re-blocked this many times it is plausibly stuck, so the judge offers a nudge.
    # Reset to 0 the moment the breaker opens. Never gates -- advisory only.
    "completion_stop_blocks": 0,
    # Host-agnostic completion-breaker stall-release (verify_state.note_completion_block).
    # completion_stall_blocks counts CONSECUTIVE completion blocks with no NET progress;
    # completion_prev_incomplete is the prior block's unresolved-task count, used to
    # detect progress. Both MUST live here so load_ledger preserves them across stops --
    # otherwise the stall counter resets every cycle and the stall-release backstop can
    # never accumulate to its cap (the backstop would be silently dead).
    "completion_stall_blocks": 0,
    "completion_prev_incomplete": None,
    "completion_best_incomplete": None,
    # Completion suicide-loop detection and judge-adjudicated lift (loop_release.py).
    "completion_prev_incomplete_set": "",
    "loop_episode_id": "",
    "loop_same_set_streak": 0,
    "loop_judge_last_at": 0.0,
    "loop_judge_episode_id": "",
    "loop_judge_at_stop_blocks": 0,
    "loop_lift_kind": "",
    "loop_lift_reason": "",
    "loop_lift_scope": "",
    "loop_lift_stops_remaining": 0,
    "loop_lift_retracted": [],
    "loop_events": [],
    # Judge-pinned grade downgrade (grade_override.py). Must be in DEFAULT_LEDGER so
    # load_ledger preserves pin state across turns and gate_prompt re-escalation.
    "grade_override_applied": False,
    "grade_override_target": "",
    "grade_override_by": "",
    "grade_override_reason": "",
    "grade_re_warrant_reason": "",
    "inject_heavy_brief": False,
    "heavy_brief_injected": False,
    "frontier_discovery_count": 0,
    "frontier_research_tools": 0,
    # Citation-verification activity log: what the session ACTUALLY did, so the
    # gate can cross-check that a spec's citations are real (see citations.py).
    # read_paths: absolute paths actually read (Read/Grep/Glob + read-style Bash).
    # fetched_urls: URLs actually fetched (WebFetch + curl/wget Bash).
    # ran_commands: Bash commands actually executed.
    # observed_tool_results: successful PostToolUse events not otherwise captured.
    "read_paths": [],
    "fetched_urls": [],
    "ran_commands": [],
    "observed_tool_results": [],
    "last_updated": "",
}

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{12,}"),
]

CODE_EXTS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".java", ".js", ".jsx", ".kt",
    ".mjs", ".php", ".py", ".rb", ".rs", ".scss", ".sh", ".sql", ".swift",
    ".ts", ".tsx",
}
DOC_EXTS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
CONFIG_EXTS = {".json", ".jsonc", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".lock"}
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf", ".mp3", ".mp4"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def redact(text: Any, limit: int = 500) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r", " ").replace("\n", " ").strip()
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    if len(value) > limit:
        return value[: limit - 3] + "..."
    return value


def data_root() -> Path:
    env_data = os.environ.get("UNIFABLE_DATA")
    base = Path(env_data).expanduser() if env_data else Path.home() / ".unifable"
    return base.resolve()


def ledger_key(input_data: dict[str, Any]) -> str:
    from spec import canonical_project_root

    cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))
    session_id = input_data.get("session_id") or "no-session"
    raw = f"{session_id}|{cwd}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def ledger_path(input_data: dict[str, Any]) -> Path:
    return data_root() / "ledgers" / f"{ledger_key(input_data)}.json"


def default_ledger() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_LEDGER)


def load_ledger(input_data: dict[str, Any]) -> dict[str, Any]:
    path = ledger_path(input_data)
    if not path.exists():
        return default_ledger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = default_ledger()
        data["failures"].append(
            {"kind": "ledger", "summary": "Ledger could not be read; continuing fresh."}
        )
        return data

    ledger = default_ledger()
    if isinstance(data, dict):
        ledger.update({key: data.get(key, value) for key, value in ledger.items()})
    for key in ("risk_flags", "change_kinds", "verification_commands", "verification_results",
                "failures", "warnings", "read_paths", "fetched_urls", "ran_commands",
                "observed_tool_results", "loop_lift_retracted", "loop_events"):
        if not isinstance(ledger.get(key), list):
            ledger[key] = []
    return ledger


def save_ledger(input_data: dict[str, Any], ledger: dict[str, Any]) -> Path:
    # Concurrent gate hooks load-modify-save this file last-writer-wins. That is
    # intentional and unlocked: the ledger is advisory, self-healing (regenerated
    # from fresh tool input each turn), dedup'd, and trimmed, so a lost update at
    # worst skips one nag this turn. Locking the per-tool-call hot path would cost
    # more than it saves. The write itself is atomic (no torn reads). Correctness-
    # critical state that accumulates (findings) is serialized instead — see
    # findings._findings_lock.
    path = ledger_path(input_data)
    ledger["last_updated"] = utc_now()
    return write_text_atomic(path, json.dumps(ledger, indent=2, sort_keys=True))


def update_ledger(input_data: dict[str, Any], updater: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    ledger = load_ledger(input_data)
    updater(ledger)
    trim_ledger(ledger)
    save_ledger(input_data, ledger)
    return ledger


def trim_ledger(ledger: dict[str, Any]) -> None:
    for key in ("risk_flags", "change_kinds"):
        values: list[Any] = []
        for value in ledger.get(key, []):
            if value not in values:
                values.append(value)
        ledger[key] = values[:20]
    for key in ("verification_commands", "verification_results", "failures", "warnings"):
        ledger[key] = ledger.get(key, [])[-40:]
    # Activity log: keep many more (citation cross-check needs the full session's
    # reads/fetches/commands), but still bound it. Newest-last, dedup'd at write.
    for key in ("read_paths", "fetched_urls", "ran_commands", "observed_tool_results"):
        ledger[key] = ledger.get(key, [])[-500:]


def add_unique(ledger: dict[str, Any], key: str, values: list[str]) -> None:
    existing = list(ledger.get(key, []))
    for value in values:
        if value and value not in existing:
            existing.append(value)
    ledger[key] = existing


def classify_path_kind(path_value: str) -> str:
    path = Path(path_value)
    name = path.name.lower()
    suffix = path.suffix.lower()
    parts = {part.lower() for part in path.parts}
    if suffix in DOC_EXTS or name in {"readme", "readme.md", "agents.md"} or "docs" in parts:
        return "docs"
    if suffix in CODE_EXTS:
        return "code"
    if suffix in CONFIG_EXTS or name.startswith(".env"):
        return "config"
    if suffix in ASSET_EXTS:
        return "assets"
    return "other"


def read_stdin_json() -> dict[str, Any]:
    import sys

    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"_parse_error": "invalid stdin json"}
    return data if isinstance(data, dict) else {"_input": data}


def emit_json(payload: dict[str, Any]) -> None:
    import sys

    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
