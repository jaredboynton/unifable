#!/usr/bin/env python3
"""Parse tool inputs/outputs into compact ledger facts (unifable observation gate).

Ported from fable-ish. Detects (a) which files changed and their kind, and
(b) whether a verification command ran and observably succeeded or failed.

Failure detection is signal-first: a tool failure is asserted ONLY from a
structured exit/success signal, or — when the host gives none — from a
high-precision anchored marker (Traceback, command-not-found, a non-zero
"exit code N", "N failed"/"N errors"). Bare words like "failed"/"failure"/
"error:" are deliberately NOT treated as failures: they appear constantly in
successful output (logs, help text, "0 failed", grep hits, this very file), and
matching them was the source of the gate's false-positive "observed a tool
failure" on Codex, whose shell tool_response is a plain output string with no
exit_code.
"""

from __future__ import annotations

import re
from typing import Any

from ledger import classify_path_kind, redact


VERIFY_RE = re.compile(
    r"(?i)\b("
    r"pytest|unittest|go\s+test|cargo\s+test|npm\s+test|pnpm\s+test|yarn\s+test|bun\s+test|"
    r"mvn\s+test|gradle\s+test|rspec|vitest|jest|playwright|cypress|"
    r"lint|eslint|ruff|flake8|mypy|pyright|tsc|typecheck|"
    r"build|check|validate|verify|json\.tool|py_compile|curl"
    r")\b"
)
# High-precision failure markers. Used only when there is no structured
# exit/success signal. Every alternative is anchored or numeric so it does not
# fire on incidental occurrences of "failed"/"failure"/"error" in successful
# output. In particular "0 failed"/"0 errors" never match (the count must be
# 1-9...), so a passing "12 passed, 0 failed" summary stays clean.
STRONG_FAILURE_RE = re.compile(
    r"(?im)("
    r"\btraceback \(most recent call last\)"            # python crash
    r"|: command not found"                              # shell: missing program
    r"|\bsegmentation fault\b|\bcore dumped\b"            # native crash
    r"|\bpanicked at\b"                                  # rust panic
    r"|^fatal: |^fatal error:"                           # git / clang
    r"|\bexit (?:code|status) [1-9][0-9]*\b"             # explicit non-zero exit
    r"|\bexited with code [1-9][0-9]*\b"
    r"|\b[1-9][0-9]*\s+(?:tests?\s+)?failed\b"           # 'N failed' (never '0 failed')
    r"|\b[1-9][0-9]*\s+(?:previous\s+)?errors?\b"        # 'N errors' / rust 'N previous errors'
    r")"
)
SUCCESS_RE = re.compile(
    r"(?i)\b(passed|success|succeeded|0 failed|0 errors?|build completed|done|valid|ok)\b"
)
MUTATING_BASH_RE = re.compile(
    r"(?i)\b(apply_patch|python\s+.*\s+-m\s+compileall|chmod|mkdir|mv|cp|rm|touch|"
    r"npm\s+run\s+build|pnpm\s+build|yarn\s+build)\b"
)


def response_text(value: Any, limit: int = 4000) -> str:
    parts: list[str] = []

    def walk(item: Any) -> None:
        if len(" ".join(parts)) > limit:
            return
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for key in ("stdout", "stderr", "output", "message", "text", "content", "error", "summary"):
                if key in item:
                    walk(item[key])
            if not parts:
                for child in item.values():
                    walk(child)
        elif isinstance(item, list):
            for child in item[:20]:
                walk(child)

    walk(value)
    return redact(" ".join(parts), limit)


def command_from_input(input_data: dict[str, Any]) -> str:
    tool_input = input_data.get("tool_input")
    if isinstance(tool_input, dict):
        return str(tool_input.get("command") or tool_input.get("description") or "")
    if isinstance(tool_input, str):
        return tool_input
    return ""


def exit_success(input_data: dict[str, Any], text: str) -> bool | None:
    """Return True/False when there is evidence, else None (unknown).

    Structured signals are authoritative. When the host gives none (e.g. Codex,
    whose shell tool_response is a bare output string), fall back only to
    high-precision markers — never to weak lexical guesses — so that "unknown"
    stays unknown instead of being mis-read as a failure.
    """
    candidates = [input_data, input_data.get("tool_response")]
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("success", "ok"):
                if isinstance(candidate.get(key), bool):
                    return bool(candidate[key])
            for key in ("exit_code", "exitCode", "returncode", "status"):
                value = candidate.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    return value == 0
                if isinstance(value, str) and value.lstrip("-").isdigit():
                    return int(value) == 0
    # No structured signal: failure takes precedence over success so a mixed
    # "1 failed, 3 passed" summary is correctly a failure.
    if STRONG_FAILURE_RE.search(text):
        return False
    if SUCCESS_RE.search(text):
        return True
    return None


def is_verification_command(command: str) -> bool:
    return bool(VERIFY_RE.search(command or ""))


def detect_failure(input_data: dict[str, Any]) -> dict[str, Any] | None:
    """A failure is asserted only on positive evidence (success is False).

    'Unknown' (no structured signal and no strong marker) is NOT a failure —
    that is the whole fix: a successful command that merely prints the word
    "failure"/"error" must not be flagged.
    """
    text = response_text(input_data.get("tool_response", input_data))
    success = exit_success(input_data, text)
    if success is False:
        return {"kind": "tool-result", "summary": redact(text or command_from_input(input_data), 240)}
    return None


def changed_paths(input_data: dict[str, Any]) -> list[str]:
    tool_name = str(input_data.get("tool_name") or "")
    tool_input = input_data.get("tool_input")
    paths: list[str] = []
    if isinstance(tool_input, dict):
        file_path = tool_input.get("file_path")
        if file_path:
            paths.append(str(file_path))
    # Codex folds edits/writes into a single `apply_patch` tool; Claude Code uses
    # Edit/Write/NotebookEdit/MultiEdit. Accept both so the gate works on either host.
    if tool_name in {"Edit", "Write", "NotebookEdit", "MultiEdit", "apply_patch"}:
        return paths or ["edit"]
    return paths


def changed_kinds(input_data: dict[str, Any]) -> list[str]:
    paths = changed_paths(input_data)
    if paths:
        return sorted({classify_path_kind(path.strip()) for path in paths})
    tool_name = str(input_data.get("tool_name") or "")
    command = command_from_input(input_data)
    if tool_name == "Bash" and MUTATING_BASH_RE.search(command):
        return ["other"]
    return []


def verification_record(input_data: dict[str, Any]) -> dict[str, Any] | None:
    command = command_from_input(input_data)
    if not command or not is_verification_command(command):
        return None
    text = response_text(input_data.get("tool_response", input_data), 1000)
    success = exit_success(input_data, text)
    return {
        "command": redact(command, 220),
        "success": bool(success) if success is not None else None,
        "summary": redact(text, 220),
    }


def _failure_signature(summary: str) -> str:
    """Normalize a failure summary into a stable class key. Numbers and paths
    differ between occurrences of the same failure, so collapse them so that
    e.g. two 'ECONNREFUSED localhost:5432' land on the same class."""
    s = (summary or "").lower()
    s = re.sub(r"[/\\][^\s]+", " path ", s)   # paths vary
    s = re.sub(r"\d+", "#", s)                # numbers vary
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80]


def repeated_failure(failures: list[dict[str, Any]], threshold: int = 2) -> tuple[str, int] | None:
    """If the most recent failure's class has occurred `threshold`+ times in the
    ledger, return (signature, count). Drives the silent-recovery guard: recover
    quietly from one-offs, but disclose a repeating failure class."""
    if not failures:
        return None
    sig = _failure_signature(failures[-1].get("summary", ""))
    if not sig:
        return None
    count = sum(1 for f in failures if _failure_signature(f.get("summary", "")) == sig)
    return (sig, count) if count >= threshold else None
