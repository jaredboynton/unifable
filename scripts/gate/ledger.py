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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    # Consecutive Stop blocks from completion_handoff.py when the agent defers
    # autonomous work. Bypasses stop_hook_active; capped at COMPLETION_HANDOFF_BLOCK_CAP.
    "completion_handoff_blocks": 0,
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
    "evidence_profile": "code",
    "inject_heavy_brief": False,
    "heavy_brief_injected": False,
    "frontier_discovery_count": 0,
    "frontier_research_tools": 0,
    # Citation-verification activity log: what the session ACTUALLY did, so the
    # gate can cross-check that a spec's citations are real (see citations.py).
    # These lists are now backed by the `activity` table in db.py (one
    # deduplicated row per value, indexed) rather than capped JSON arrays;
    # load_ledger rehydrates them and save_ledger appends them idempotently.
    # read_paths: absolute paths actually read (Read/Grep/Glob + read-style Bash).
    # fetched_urls: URLs actually fetched (WebFetch + curl/wget Bash).
    # ran_commands: Bash commands actually executed.
    "read_paths": [],
    "fetched_urls": [],
    "ran_commands": [],
    # tool_evidence: richer "<tool>: <summary>" entries for MCP tool calls
    # (Slack/Jira/GitHub/etc.). MCP results are the real evidence corpus for
    # research tasks, so they are surfaced to the Stop validation judge.
    "tool_evidence": [],
    # command_outputs: "<command>: <compressed output>" for successful generic
    # shell calls (curl probes, cat of a config, etc.). ran_commands records the
    # command STRING only; this carries the OUTPUT so the Stop evidence_only judge
    # can see probe proof instead of relying on the budget-capped transcript tail.
    "command_outputs": [],
    # PreToolUse block dedup (pretool_block.py): one full stderr message per
    # (epoch, signature) when parallel hooks fire on the same turn.
    "pretool_block_epoch": "",
    "pretool_block_counts": {},
    "pretool_last_block_kind": "",
    "pretool_last_block_detail": "",
    # Host Plan Mode (plan_mode.py), set at UserPromptSubmit, read at PreToolUse.
    "plan_mode_enabled": False,
    "plan_mode_host": "",
    "plan_mode_notified_epoch": "",
    # UserPromptSubmit scaffold onboarding (gate_prompt.py): full CLI tutorial once.
    "prompt_scaffold_notified": False,
    "citation_footer_notified": False,
    # UserPromptSubmit router pack dedup (pack_router.py).
    "router_matched_tags": [],
    "router_fired_tags": [],
    # PreToolUse unlock footer dedup (pretool_block.py).
    "pretool_unlock_footer_epoch": "",
    "pretool_allowlist_notified_epoch": "",
    # PreToolUse spec validation contract string once per turn (spec.py).
    "spec_contract_notified_epoch": "",
    # PostToolUse additionalContext dedup (posttool_notify.py / model_notify.py).
    "posttool_context_epoch": "",
    "posttool_last_body_hash": "",
    "posttool_task_guidance": {},
    "posttool_last_breaker_status": "",
    "posttool_last_failure_hint_hash": "",
    "posttool_last_discovery_headline": "",
    # gpt-realtime-2 judge token-usage accounting (judge_usage.record_usage).
    # Measures prompt-cache effectiveness: judge_cached_tokens / judge_input_tokens
    # is the realized cache-hit rate the caching rearchitecture optimizes for.
    "judge_calls": 0,
    "judge_input_tokens": 0,
    "judge_cached_tokens": 0,
    "judge_output_tokens": 0,
    "judge_last_usage": {},
    "last_updated": "",
}

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{12,}"),
]

CODE_EXTS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".mjs",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
}
DOC_EXTS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
CONFIG_EXTS = {".json", ".jsonc", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".lock"}
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".pdf", ".mp3", ".mp4"}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


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
    from spec_io import canonical_project_root

    cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))
    session_id = input_data.get("session_id") or "no-session"
    raw = f"{session_id}|{cwd}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]


def ledger_path(input_data: dict[str, Any]) -> Path:
    return data_root() / "ledgers" / f"{ledger_key(input_data)}.json"


def default_ledger() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_LEDGER)


def _import_legacy_ledger(input_data: dict[str, Any], skey: str) -> dict[str, Any] | None:
    """One-time import of a legacy ``ledgers/{key}.json`` into the DB when no DB
    row exists yet. Returns the imported dict, or None when no legacy file."""
    path = ledger_path(input_data)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        import db

        db.session_save(
            skey,
            data,
            session_id=str(input_data.get("session_id") or ""),
            project_root=str(input_data.get("cwd") or ""),
        )
    except Exception:
        pass
    return data


def load_ledger(input_data: dict[str, Any]) -> dict[str, Any]:
    # Storage is the consolidated SQLite DB (db.sessions + db.activity); the
    # legacy per-session JSON file is imported once on first miss. Any DB error
    # fails open to a fresh default ledger -- the gate never hard-locks on its own
    # storage bug.
    try:
        import db

        skey = ledger_key(input_data)
        data = db.session_load(skey)
        if data is None:
            data = _import_legacy_ledger(input_data, skey)
    except Exception:
        data = None

    ledger = default_ledger()
    if isinstance(data, dict):
        ledger.update({key: data.get(key, value) for key, value in ledger.items()})
    for key in (
        "risk_flags",
        "change_kinds",
        "verification_commands",
        "verification_results",
        "failures",
        "warnings",
        "read_paths",
        "fetched_urls",
        "ran_commands",
        "tool_evidence",
        "command_outputs",
        "loop_lift_retracted",
        "loop_events",
        "router_matched_tags",
        "router_fired_tags",
    ):
        if not isinstance(ledger.get(key), list):
            ledger[key] = []
    if not isinstance(ledger.get("pretool_block_counts"), dict):
        ledger["pretool_block_counts"] = {}
    return ledger


def save_ledger(input_data: dict[str, Any], ledger: dict[str, Any]) -> Path:
    # Storage is the consolidated SQLite DB. The scalar "soup" is last-writer-wins
    # on sessions.data (the historically-accepted semantics for this advisory,
    # self-healing state); the activity lists are appended idempotently to the
    # `activity` table (one deduplicated row per value). WAL serializes writers for
    # microseconds, so the per-tool-call hot path stays cheap. Returns the legacy
    # path purely for signature compatibility with existing callers.
    ledger["last_updated"] = utc_now()
    try:
        import db

        db.session_save(
            ledger_key(input_data),
            ledger,
            session_id=str(input_data.get("session_id") or ""),
            project_root=str(input_data.get("cwd") or ""),
        )
    except Exception:
        pass
    return ledger_path(input_data)


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
    # Activity log lists are backed by the db.activity table (deduplicated rows,
    # no array to cap). Keep an in-memory bound only so a single load-modify-save
    # cycle does not balloon the dict; the DB itself holds the full session set.
    for key in ("read_paths", "fetched_urls", "ran_commands", "tool_evidence", "command_outputs"):
        ledger[key] = ledger.get(key, [])[-2000:]


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
