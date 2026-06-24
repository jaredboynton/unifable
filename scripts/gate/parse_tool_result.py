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
import shlex
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

# File-write tools: tool_response carries written file content, not command
# output, so failure is never inferred from its text (see detect_failure).
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Content tools: tool_response is file/page DATA, not a command result. Their text
# legitimately contains "failed"/"Traceback"/"exit code N" (docs, source, this very
# parser), so failure must never be inferred from it -- same reasoning as
# WRITE_TOOLS. Scanning them was double trouble: a spurious "tool failure" message
# AND silently dropping the read/fetch from the citation ledger.
CONTENT_TOOLS = frozenset({"Read", "WebFetch", "WebSearch", "Grep", "Glob", "NotebookRead", "FetchMcpResource"})
_FETCH_TOOL_RE = re.compile(
    r"(?i)(?:webfetch|fetchmcpresource|mcp.*fetch|exa|browser|web.?fetch|mcp_resource)",
)
_URL_INPUT_KEYS = ("url", "urls", "uri", "href", "link", "page_url", "website", "source")
COMMAND_RESULT_TOOLS = {"Bash", "apply_patch"}

# A host-reported exit status embedded in command output (Claude Code prefixes Bash
# output with "Bash exited with code N"). Authoritative when present.
_EXIT_CODE_RE = re.compile(r"(?i)\bexit(?:ed)?(?: with)? (?:code|status) (\d+)\b")


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


# ---------------------------------------------------------------------------
# Citation-verification activity extractors (what the session ACTUALLY accessed)
# ---------------------------------------------------------------------------
# Used by gate_post_tool (live, into the ledger) AND citations.scan_transcript
# (replaying a transcript). The structured tools (Read/Grep/Glob/WebFetch) are
# trusted directly. Bash parsing is HARDENED against fabrication: comments are
# stripped, and a read/fetch program only counts when it is the COMMAND of a
# shell segment (its first token) -- never a string mentioned anywhere. So
# `# cat secret.py`, `echo https://x`, and `awk 'BEGIN{print "/p"}'` register
# NOTHING, while `cat f`, `cd x && grep p f`, `curl https://x` register the real
# target. gate_post_tool additionally drops anything from a command that failed.

# read-style programs with a clean `prog [flags] FILE...` shape. Script-taking
# readers (awk/sed/jq/yq) are deliberately excluded: their first arg is code,
# not a file, so path extraction would be fabricable.
_READ_PROGS = {"cat", "bat", "head", "tail", "nl", "less", "more",
               "grep", "egrep", "fgrep", "rg", "ag", "ack", "xmllint"}
# grep-family: the first non-flag arg is the PATTERN, not a file -- skip it.
_GREP_FAMILY = {"grep", "egrep", "fgrep", "rg", "ag", "ack"}
# fetch-style programs (URL retrievers). Matched in command position only.
_FETCH_PROGS = {"curl", "wget", "xh", "http", "https", "httpie", "lynx", "w3m"}
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s'\"|>;)]+", re.IGNORECASE)
_SHELL_OPERATORS = {";", "|", "||", "&&", "&", ">", ">>", "<", "2>", "2>&1"}


def _tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


_SEG_SPLIT_RE = re.compile(r"[\n\r]|&&|\|\||;|\||&")

# Leading redirection operator on a token: optional leading fd, the >/>>/</<<
# operator, and an optional fd-dup (&N) or trailing fd. Matches `>`, `2>`, `2>>`,
# `<`, `2>&1`, `>&2`, `&>`. A non-empty remainder after the match is an attached
# target (e.g. `2>/dev/null`); an empty remainder on a non-fd-dup operator means
# the filename target is the next token.
_REDIRECT_OP_RE = re.compile(r"^(&?\d*(?:>>?|<<?)&?\d*)")


def _drop_redirections(toks: list[str]) -> list[str]:
    """Remove shell redirections and bare control operators so a redirect target
    (`2>/dev/null`, `>out.txt`, or a separated `2> /dev/null`) is never mistaken
    for a read file or fetch URL. Fd-dups (`2>&1`, `>&2`) carry no filename target."""
    out: list[str] = []
    expect_target = False
    for t in toks:
        if expect_target:
            expect_target = False  # this token is the redirect's filename target
            continue
        m = _REDIRECT_OP_RE.match(t)
        if m:
            op = m.group(1)
            rest = t[m.end():]
            is_fd_dup = bool(re.search(r"[<>]&", op))
            if rest == "" and not is_fd_dup:
                expect_target = True  # bare `>` / `2>` -> next token is the target
            continue
        if t in _SHELL_OPERATORS:
            continue
        out.append(t)
    return out


def _command_segments(cmd: str) -> list[list[str]]:
    """Split a shell command into program-led segments. Splits on NEWLINES and
    shell operators (&&, ||, ;, |, &) -- so a read/fetch on its own line in a
    multi-line script is still seen in command position -- then tokenizes each
    segment with `# ...` comments stripped (so a comment can never inject a
    phantom read/fetch) and redirections dropped (so `2>/dev/null` is never read
    as a file)."""
    segments: list[list[str]] = []
    for raw in _SEG_SPLIT_RE.split(cmd):
        raw = raw.strip()
        if not raw:
            continue
        try:
            toks = shlex.split(raw, comments=True)
        except ValueError:
            toks = [t for t in raw.split() if not t.startswith("#")]
        toks = _drop_redirections(toks)
        if toks:
            segments.append(toks)
    return segments


def _bash_read_files(cmd: str) -> list[str]:
    """File arguments of read-style commands, only when the program is in command
    position. grep-family: the pattern arg is skipped. Flags, URLs, and code-shaped
    tokens are ignored."""
    out: list[str] = []
    for seg in _command_segments(cmd):
        if not seg:
            continue
        prog = seg[0].rsplit("/", 1)[-1]
        if prog not in _READ_PROGS:
            continue
        skip_pattern = prog in _GREP_FAMILY
        for arg in seg[1:]:
            if arg.startswith("-") or arg in _SHELL_OPERATORS:
                continue
            if skip_pattern:
                skip_pattern = False  # this token is the search pattern, not a file
                continue
            if "://" in arg:
                continue
            if "/" in arg or "." in arg:
                out.append(arg)
    return out


def _bash_fetch_urls(cmd: str) -> list[str]:
    """URLs of fetch-style commands, only when the program is in command position."""
    out: list[str] = []
    for seg in _command_segments(cmd):
        if not seg:
            continue
        prog = seg[0].rsplit("/", 1)[-1]
        if prog not in _FETCH_PROGS:
            continue
        for arg in seg[1:]:
            out.extend(_URL_IN_TEXT_RE.findall(arg))
    return out


def _urls_from_mapping(obj: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in _URL_INPUT_KEYS:
        value = obj.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            out.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.startswith(("http://", "https://")):
                    out.append(item)
                elif isinstance(item, dict):
                    out.extend(_urls_from_mapping(item))
    return out


def _urls_from_any(value: Any, *, limit: int = 20) -> list[str]:
    """Recursively extract http(s) URLs from tool payloads (MCP/Exa/browser results)."""
    out: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        for match in _URL_IN_TEXT_RE.findall(raw):
            if match not in seen and len(out) < limit:
                seen.add(match)
                out.append(match)

    def walk(item: Any) -> None:
        if len(out) >= limit:
            return
        if isinstance(item, str):
            add(item)
        elif isinstance(item, dict):
            out.extend(_urls_from_mapping(item))
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item[:50]:
                walk(child)

    walk(value)
    return out[:limit]


def _is_fetch_tool(tool: str) -> bool:
    if tool in ("WebFetch", "FetchMcpResource"):
        return True
    return bool(_FETCH_TOOL_RE.search(tool))


def _is_content_tool(tool: str) -> bool:
    return tool in CONTENT_TOOLS or _is_fetch_tool(tool)


def read_targets(input_data: dict[str, Any]) -> list[str]:
    """Files this tool call actually read: Read.file_path, Grep/Glob.path,
    NotebookRead.notebook_path, and command-position read-style Bash."""
    tool = str(input_data.get("tool_name") or "")
    ti = input_data.get("tool_input")
    out: list[str] = []
    if isinstance(ti, dict):
        if tool in ("Read", "Grep", "Glob", "NotebookRead"):
            for key in ("file_path", "path", "notebook_path"):
                value = ti.get(key)
                if value:
                    out.append(str(value))
        if tool == "Bash":
            out.extend(_bash_read_files(str(ti.get("command") or "")))
    elif isinstance(ti, str) and tool == "Bash":
        out.extend(_bash_read_files(ti))
    return [p for p in out if p]


def fetched_url_targets(input_data: dict[str, Any]) -> list[str]:
    """URLs this tool call actually fetched: WebFetch.url, MCP/Exa/browser payloads,
    and command-position fetch Bash. WebSearch alone is excluded: a query is not
    proof a source was read unless the response carries source URLs."""
    tool = str(input_data.get("tool_name") or "")
    ti = input_data.get("tool_input")
    out: list[str] = []
    if isinstance(ti, dict):
        if tool == "WebFetch" and ti.get("url"):
            out.append(str(ti.get("url")))
        out.extend(_urls_from_mapping(ti))
        if tool == "Bash":
            out.extend(_bash_fetch_urls(str(ti.get("command") or "")))
    elif isinstance(ti, str) and tool == "Bash":
        out.extend(_bash_fetch_urls(ti))
    if _is_fetch_tool(tool):
        out.extend(_urls_from_any(input_data.get("tool_response")))
        out.extend(_urls_from_any(ti))
    elif tool == "WebSearch":
        # Search snippets may cite sources; record URLs present in the response.
        out.extend(_urls_from_any(input_data.get("tool_response")))
    # dedupe, preserve order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def ran_command(input_data: dict[str, Any]) -> str | None:
    """The Bash command this tool call executed (normalized), else None."""
    if str(input_data.get("tool_name") or "") != "Bash":
        return None
    cmd = command_from_input(input_data).strip()
    return cmd or None


def command_from_input(input_data: dict[str, Any]) -> str:
    tool_input = input_data.get("tool_input")
    if isinstance(tool_input, dict):
        return str(tool_input.get("command") or tool_input.get("description") or "")
    if isinstance(tool_input, str):
        return tool_input
    return ""


def exit_success(input_data: dict[str, Any], text: str, scan_text: bool = True) -> bool | None:
    """Return True/False when there is evidence, else None (unknown).

    Structured signals are authoritative. When the host gives none (e.g. Codex,
    whose shell tool_response is a bare output string), fall back only to
    high-precision markers — never to weak lexical guesses — so that "unknown"
    stays unknown instead of being mis-read as a failure. `scan_text=False`
    disables the lexical fallback entirely (used for file-write tools, whose
    "output" is arbitrary file content, not command output).
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
    if not scan_text:
        return None
    # Claude Code's shell tool_response is a structured dict {stdout, stderr,
    # interrupted, ...} with NO exit code, and PostToolUse fires only AFTER a tool
    # SUCCEEDS -- so this output is from a command that already exited 0. A marker
    # in it ("1 failed", "Traceback") is DATA the command printed, never the
    # command's own failure; scanning it was the false-positive source. Only an
    # explicit interrupt counts as failure for this shape.
    tr = input_data.get("tool_response")
    if isinstance(tr, dict) and ("stdout" in tr or "stderr" in tr):
        return False if tr.get("interrupted") is True else None
    # Bare output string (e.g. Codex shell): a host-reported exit status embedded
    # in the text is authoritative ("Exit code N" / "exited with code N").
    m = _EXIT_CODE_RE.search(text)
    if m:
        return int(m.group(1)) == 0
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

    File-write tools (Edit/Write/NotebookEdit/MultiEdit) echo the WRITTEN FILE
    CONTENT as their response. That content legitimately contains strings like
    "exit code 2" or "3 failed" (e.g. docs, tests, this parser itself), so text
    scanning is disabled for them — only a structured error signal can mark a
    write as failed. Command/exec tools (Bash, apply_patch) keep text scanning.
    """
    tool_name = str(input_data.get("tool_name") or "")
    text = response_text(input_data.get("tool_response", input_data))
    # Never text-scan file-write or content tools: their response is data, not a
    # command result. Only a structured error signal can mark them failed.
    no_scan = tool_name in WRITE_TOOLS or _is_content_tool(tool_name) or tool_name not in COMMAND_RESULT_TOOLS
    success = exit_success(input_data, text, scan_text=not no_scan)
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
