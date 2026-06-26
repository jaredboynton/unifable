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

from ledger import SECRET_PATTERNS, classify_path_kind, redact

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
    r"\btraceback \(most recent call last\)"  # python crash
    r"|: command not found"  # shell: missing program
    r"|\bsegmentation fault\b|\bcore dumped\b"  # native crash
    r"|\bpanicked at\b"  # rust panic
    r"|^fatal: |^fatal error:"  # git / clang
    r"|\bexit (?:code|status) [1-9][0-9]*\b"  # explicit non-zero exit
    r"|\bexited with code [1-9][0-9]*\b"
    r"|\b[1-9][0-9]*\s+(?:tests?\s+)?failed\b"  # 'N failed' (never '0 failed')
    r"|\b[1-9][0-9]*\s+(?:previous\s+)?errors?\b"  # 'N errors' / rust 'N previous errors'
    r")"
)
SUCCESS_RE = re.compile(r"(?i)\b(passed|success|succeeded|0 failed|0 errors?|build completed|done|valid|ok)\b")
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
    r"(?i)(?:webfetch|fetchmcpresource|mcp.*fetch|exa|browser|web.?fetch|mcp_resource|web_fetch)",
)
_URL_INPUT_KEYS = ("url", "urls", "uri", "href", "link", "page_url", "website", "source")
SHELL_TOOLS = frozenset({"Bash", "REPL", "exec_command", "Shell"})
REPL_JS_TOOLS = frozenset({"exec", "js", "javascript", "node_repl__js", "mcp__node_repl__js"})
STRUCTURED_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "NotebookRead"})
COMMAND_RESULT_TOOLS = {"Bash", "REPL", "exec_command", "Shell", "apply_patch"}

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
_READ_PROGS = {"cat", "bat", "head", "tail", "wc", "nl", "less", "more", "grep", "egrep", "fgrep", "rg", "ag", "ack", "xmllint"}
# grep-family: the first non-flag arg is the PATTERN, not a file -- skip it.
_GREP_FAMILY = {"grep", "egrep", "fgrep", "rg", "ag", "ack"}
_BARE_FILENAME_RE = re.compile(r"^[\w@+,-]+$")
# fetch-style programs (URL retrievers). Matched in command position only.
_FETCH_PROGS = {"curl", "wget", "xh", "http", "https", "httpie", "lynx", "w3m"}
_PATH_INPUT_KEYS = ("file_path", "path", "notebook_path")
_MCP_WRITE_RE = re.compile(r"(?i)(write|create|update|delete|patch|apply|remove|insert|put|post|send)")
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
            rest = t[m.end() :]
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
            if arg.endswith("/"):
                continue  # directory search root (e.g. `rg pat scripts/gate/`), not a file read
            if "/" in arg or "." in arg or _looks_like_bare_filename(arg):
                out.append(arg)
    return out


def _looks_like_bare_filename(token: str) -> bool:
    """True for cwd-relative single-segment names like justfile or Makefile."""
    return len(token) >= 2 and bool(_BARE_FILENAME_RE.match(token))


def _looks_like_path_string(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw or "://" in raw:
        return False
    if raw.startswith("file:"):
        return len(raw) > 5
    return bool(raw)


def _paths_from_mapping(obj: Any, *, depth: int = 0) -> list[str]:
    """Structured path keys from tool_input (MCP queries[].path, Read.path, etc.)."""
    if depth > 6:
        return []
    out: list[str] = []
    if isinstance(obj, dict):
        for key in _PATH_INPUT_KEYS:
            value = obj.get(key)
            if isinstance(value, str) and _looks_like_path_string(value):
                out.append(value)
        queries = obj.get("queries")
        if isinstance(queries, list):
            for item in queries:
                if isinstance(item, dict):
                    path = item.get("path")
                    if isinstance(path, str) and _looks_like_path_string(path):
                        out.append(path)
        for key, child in obj.items():
            if key == "queries":
                continue
            if isinstance(child, (dict, list)):
                out.extend(_paths_from_mapping(child, depth=depth + 1))
    elif isinstance(obj, list):
        for child in obj[:50]:
            if isinstance(child, (dict, list)):
                out.extend(_paths_from_mapping(child, depth=depth + 1))
    return out


def _mcp_tool_is_read_like(tool_name: str) -> bool:
    name = str(tool_name or "")
    if not is_mcp_tool(name):
        return False
    if _MCP_WRITE_RE.search(name):
        return False
    return bool(re.search(r"(?i)(read|get|fetch|content|search|file|view|lookup|list|query)", name))


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


# MCP tool results (Slack/Jira/GitHub/Salesforce/etc.) ARE the evidence corpus for
# research tasks, so the gate captures them as first-class evidence rather than the
# 180-char observed snippet. Claude surfaces MCP tools as "mcp__<server>__<tool>";
# Codex surfaces them as "<server>.<tool>". Core host tools (Read, Bash, apply_patch,
# Edit, ...) carry neither shape, so this stays precise.
MCP_EVIDENCE_CHARS = 700
RESEARCH_BASH_EVIDENCE_CHARS = 4000


def _explore_script_in_bash_command(command: str) -> str | None:
    try:
        from bash_classify import explore_script_in_command
    except ImportError:  # pragma: no cover
        from scripts.gate.bash_classify import explore_script_in_command
    return explore_script_in_command(command)


def is_mcp_tool(tool_name: str) -> bool:
    name = str(tool_name or "")
    if name.startswith("mcp__"):
        return True
    return "." in name and "/" not in name and " " not in name and not name.endswith(".")


def mcp_evidence(input_data: dict[str, Any], limit: int = MCP_EVIDENCE_CHARS) -> str | None:
    """A compact "<tool>: <result>" evidence line for an MCP tool call, else None.

    Captured into ledger['tool_evidence'] and surfaced to the Stop validation
    judge so a research requirement can be adjudicated against the actual
    retrieval (e.g. the Slack message / PR metadata the model saw)."""
    tool = str(input_data.get("tool_name") or "")
    if not is_mcp_tool(tool):
        return None
    body = response_text(input_data.get("tool_response", input_data), limit)
    body = redact(body, limit).strip()
    if not body:
        return None
    return f"{tool}: {body}"


# --------------------------------------------------------------------------- #
# Research-output compression (explore trace.sh / websearch.sh stdout)
# --------------------------------------------------------------------------- #
# The legacy path fed this stdout through ledger.redact, which flattens newlines
# and HEAD-truncates to the budget. trace.sh/websearch.sh put their high-signal
# payload -- source URLs, file:line code refs, and the closing Recommendation /
# Key-files summary -- across the body and at the TAIL, so head-truncation drops
# exactly what an evidence_only research judge adjudicates against, and the
# flatten erases its section structure. Benchmarked against the alternatives
# (tail-only, head+tail) on real explore out.md files, an order-preserving
# salience filter wins on signal recall at the same budget -- see
# tests/test_research_evidence_compress.py.
_RESEARCH_GATHER_CAP = 200_000  # never pre-truncate realistic explore output before the filter
_RB_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_RB_FENCE_REF_RE = re.compile(r"^\s*`{3,}[\w.+-]*\s*\d+:\d+:[^\s`]+")  # ```120:140:path opener
_RB_INLINE_REF_RE = re.compile(r"(?<![:\w/])(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z]{1,6}:\d+\b")
_RB_URL_RE = re.compile(r"https?://[^\s)>\]\"'|]+")
_RB_WIRE_FILE_RE = re.compile(r"<file:[^:>]+:\d+-\d+>")
_RB_WIRE_URL_RE = re.compile(r"<url:https?://[^>]+>")
_RB_WIRE_QUOTE_RE = re.compile(r"<quote:https?://[^>|]+\|")
_RB_WIRE_SECTION_RE = re.compile(r"^SECTION\s+[A-Za-z]", re.M)
_RB_TABLE_RE = re.compile(r"^\s*\|.*\|\s*$")
_RB_KEYWORD_RE = re.compile(
    r"(?i)\b(recommendation|verified fact|key files?|summary|conclusion|caveat|gotcha|takeaway|important)\b"
)
_RB_MERMAID_OPEN_RE = re.compile(r"^\s*`{3,}\s*mermaid\b")
_RB_FENCE_CLOSE_RE = re.compile(r"^\s*`{3,}\s*$")


def _gather_response_text(value: Any, cap: int) -> str:
    """Collect stdout/stderr/etc. joined by NEWLINES (not flattened), bounded by cap.

    Mirrors response_text's field walk but preserves line structure and applies
    no truncation/redaction -- compress_research_output owns those."""
    parts: list[str] = []

    def walk(item: Any) -> None:
        if sum(len(p) for p in parts) > cap:
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
    return "\n".join(parts)[:cap]


def _redact_keep_lines(text: str) -> str:
    """Apply SECRET_PATTERNS line-wise, preserving newlines (unlike ledger.redact)."""
    out: list[str] = []
    for line in str(text or "").splitlines():
        v = line
        for pattern in SECRET_PATTERNS:
            v = pattern.sub("[REDACTED]", v)
        out.append(v)
    return "\n".join(out)


def compress_research_output(text: str, budget: int = RESEARCH_BASH_EVIDENCE_CHARS) -> str:
    """Budget-bounded, newline-preserving, secret-redacted research-evidence text.

    Order-preserving salience filter: keeps the opening summary, the closing
    conclusion (last header to EOF), and every high-signal line (URLs, file:line
    code refs, section headers, table rows, keyword lines); collapses mermaid
    bodies; fills the remaining budget with surrounding prose and marks dropped
    runs. A final head-preserving tail-trim backstops the budget. Returns the
    redacted text unchanged when it already fits."""
    if budget <= 0:
        return ""
    s = _redact_keep_lines(text)
    if len(s) <= budget:
        return s
    lines = s.split("\n")

    # mermaid block bodies -> single placeholder line
    masked: list[str | None] = []
    in_mermaid = False
    for ln in lines:
        if _RB_MERMAID_OPEN_RE.match(ln):
            in_mermaid = True
            masked.append(ln)
            continue
        if in_mermaid:
            if _RB_FENCE_CLOSE_RE.match(ln):
                in_mermaid = False
                masked.append("[mermaid diagram omitted]")
                masked.append(ln)
            else:
                masked.append(None)
            continue
        masked.append(ln)
    lines = [x for x in masked if x is not None]
    n = len(lines)

    nonempty = [i for i, ln in enumerate(lines) if ln.strip()]
    head_idx = set(nonempty[:4])
    critical = set(head_idx) | set(nonempty[-14:])
    last_hdr = max((i for i, ln in enumerate(lines) if _RB_HEADER_RE.match(ln)), default=None)
    if last_hdr is not None:
        critical |= set(range(last_hdr, n))

    def tier(i: int, ln: str) -> int:
        if i in critical:
            return 0
        if _RB_URL_RE.search(ln) or _RB_FENCE_REF_RE.match(ln) or _RB_INLINE_REF_RE.search(ln):
            return 0
        if _RB_WIRE_FILE_RE.search(ln) or _RB_WIRE_URL_RE.search(ln) or _RB_WIRE_QUOTE_RE.search(ln):
            return 0
        if _RB_WIRE_SECTION_RE.match(ln):
            return 0
        if _RB_HEADER_RE.match(ln) or _RB_TABLE_RE.match(ln) or _RB_KEYWORD_RE.search(ln):
            return 1
        return 2

    tiers = [tier(i, ln) for i, ln in enumerate(lines)]
    keep = [False] * n
    used = 0
    for want in (0, 1, 2):
        for i in range(n):
            if keep[i] or tiers[i] != want:
                continue
            cost = len(lines[i]) + 1
            if used + cost > budget:
                if want == 0:
                    continue  # skip an oversized critical line, keep scanning
                break
            keep[i] = True
            used += cost
        if want != 0 and used >= budget:
            break

    out: list[str] = []
    run = 0
    for i in range(n):
        if keep[i]:
            if run:
                out.append(f"[... {run} lines ...]")
                run = 0
            out.append(lines[i])
        else:
            run += 1
    if run:
        out.append(f"[... {run} lines ...]")
    result = "\n".join(out)
    if len(result) > budget:  # backstop: keep the head, tail-trim the rest
        head = "\n".join(lines[i] for i in sorted(head_idx))
        marker = "\n[... omitted ...]\n"
        room = budget - len(head) - len(marker)
        if room > 0:
            result = head + marker + result[-room:]
        else:
            result = result[: max(0, budget - 3)] + "..."
    return result


def is_shell_tool(tool_name: str) -> bool:
    name = str(tool_name or "")
    return name in SHELL_TOOLS or name in REPL_JS_TOOLS


def is_repl_tool(tool_name: str) -> bool:
    name = str(tool_name or "")
    return name == "REPL" or name in REPL_JS_TOOLS


_REPL_READ_PATH_RE = re.compile(
    r"(?:Read|Grep|Glob|NotebookRead)\s*\(\s*\{[^}]*?"
    r"(?:file_path|path|notebook_path)\s*:\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)
_REPL_WEBFETCH_URL_RE = re.compile(
    r"WebFetch\s*\(\s*\{[^}]*?url\s*:\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)
_REPL_CAT_RE = re.compile(r"(?:^|[^A-Za-z0-9_$])cat\s*\(\s*['\"]([^'\"]+)['\"]")
_REPL_BASH_CMD_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_$])(?:Bash|sh)\s*\(\s*\{[^}]*command\s*:\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)
_REPL_EXEC_CMD_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_$])(?:tools\.)?exec_command\s*\(\s*\{[^}]*?\bcmd\s*:\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)
_REPL_VIEW_IMAGE_PATH_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_$])(?:tools\.)?view_image\s*\(\s*\{[^}]*?\bpath\s*:\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)
_REPL_TOOL_PATH_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_$])tools\.[\w$]+\s*\(\s*\{[^}]*?"
    r"(?:file_path|path|notebook_path)\s*:\s*['\"]([^'\"]+)['\"]",
    re.DOTALL,
)


def repl_shell_cmds_from_code(code: str) -> list[str]:
    """Shell commands embedded in REPL / Codex exec JS source."""
    out: list[str] = []
    for pattern in (_REPL_BASH_CMD_RE, _REPL_EXEC_CMD_RE):
        out.extend(m.group(1) for m in pattern.finditer(code))
    return out


def repl_code_from_input(input_data: dict[str, Any]) -> str:
    """REPL / JS-REPL source text from tool_input (code, input, or bare str)."""
    ti = input_data.get("tool_input")
    if isinstance(ti, dict):
        for key in ("code", "input", "script", "source"):
            value = ti.get(key)
            if isinstance(value, str) and value.strip():
                return value
    if isinstance(ti, str):
        return ti
    return ""


def _paths_from_structured_tool(name: str, ti: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if name in STRUCTURED_READ_TOOLS or name == "view_image":
        out.extend(_paths_from_mapping(ti))
    if is_mcp_tool(name) and _mcp_tool_is_read_like(name):
        out.extend(_paths_from_mapping(ti))
    if name == "Bash":
        out.extend(_bash_read_files(str(ti.get("command") or "")))
    return out


def _urls_from_structured_tool(name: str, ti: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if name == "WebFetch" and ti.get("url"):
        out.append(str(ti["url"]))
    if name == "Bash":
        out.extend(_bash_fetch_urls(str(ti.get("command") or "")))
    return out


def _walk_nested_tool_activity(value: Any, reads: list[str], fetches: list[str]) -> None:
    """Extract nested Read/Bash/WebFetch tool_use blocks from REPL tool_response."""
    if isinstance(value, dict):
        name = str(value.get("name") or "")
        inp = value.get("input") or value.get("tool_input") or {}
        if isinstance(inp, dict) and name:
            reads.extend(_paths_from_structured_tool(name, inp))
            fetches.extend(_urls_from_structured_tool(name, inp))
        for child in value.values():
            _walk_nested_tool_activity(child, reads, fetches)
    elif isinstance(value, list):
        for child in value:
            _walk_nested_tool_activity(child, reads, fetches)


def _paths_from_repl_code(code: str) -> list[str]:
    out: list[str] = []
    for m in _REPL_READ_PATH_RE.finditer(code):
        out.append(m.group(1))
    for m in _REPL_CAT_RE.finditer(code):
        out.append(m.group(1))
    for m in _REPL_VIEW_IMAGE_PATH_RE.finditer(code):
        out.append(m.group(1))
    for m in _REPL_TOOL_PATH_RE.finditer(code):
        out.append(m.group(1))
    for cmd in repl_shell_cmds_from_code(code):
        out.extend(_bash_read_files(cmd))
    return out


def _urls_from_repl_code(code: str) -> list[str]:
    out: list[str] = []
    for m in _REPL_WEBFETCH_URL_RE.finditer(code):
        out.append(m.group(1))
    for cmd in repl_shell_cmds_from_code(code):
        out.extend(_bash_fetch_urls(cmd))
    return out


def repl_nested_activity(input_data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Read paths and fetch URLs implied by a REPL tool call (code + nested results)."""
    reads: list[str] = []
    fetches: list[str] = []
    code = repl_code_from_input(input_data)
    if code:
        reads.extend(_paths_from_repl_code(code))
        fetches.extend(_urls_from_repl_code(code))
    _walk_nested_tool_activity(input_data.get("tool_response"), reads, fetches)
    return reads, fetches


def research_bash_evidence(input_data: dict[str, Any], limit: int = RESEARCH_BASH_EVIDENCE_CHARS) -> str | None:
    """Compact ``<script>: <result>`` evidence for explore trace.sh/websearch.sh Bash.

    Captured into ledger['tool_evidence'] and surfaced to the Stop validation
    judge so research requirements can be adjudicated against the actual
    retrieval output, not just the command string in ran_commands. The stdout is
    compressed with compress_research_output (newline-preserving, secret-redacted,
    salience-budgeted) instead of the legacy redact flatten+head-truncate."""
    if not is_shell_tool(str(input_data.get("tool_name") or "")):
        return None
    cmd = command_from_input(input_data).strip()
    if not cmd:
        return None
    script = _explore_script_in_bash_command(cmd)
    if not script:
        return None
    raw = _gather_response_text(input_data.get("tool_response", input_data), _RESEARCH_GATHER_CAP)
    body = compress_research_output(raw, limit).strip()
    if not body:
        return None
    return f"{script}: {body}"


COMMAND_OUTPUT_EVIDENCE_CHARS = 2000


def command_output_evidence(input_data: dict[str, Any], limit: int = COMMAND_OUTPUT_EVIDENCE_CHARS) -> str | None:
    """Compact ``<command>: <output>`` evidence for a successful generic shell call.

    Closes the Stop-gate gap where ran_commands recorded the command STRING but not
    its output: a probe like `curl ...` (HTTP body) or `cat catalog.json` (an ETag)
    is neither MCP, an explore script, nor a VERIFY_RE command, so its output lived
    only in the budget-capped transcript tail and the evidence_only judge could not
    see the proof. Captured into ledger['command_outputs'] and surfaced to the Stop
    validation judge as its own corpus category.

    Deferred to research_bash_evidence for explore trace.sh/websearch.sh (richer
    capture) to avoid double-recording. Output is compressed with
    compress_research_output (newline-preserving, secret-redacted, salience-budgeted).
    Returns None for non-shell tools, empty output, or explore-script commands."""
    if not is_shell_tool(str(input_data.get("tool_name") or "")):
        return None
    cmd = command_from_input(input_data).strip()
    if not cmd:
        return None
    if _explore_script_in_bash_command(cmd):
        return None  # research_bash_evidence owns explore-script output
    raw = _gather_response_text(input_data.get("tool_response", input_data), _RESEARCH_GATHER_CAP)
    cmd_label = " ".join(cmd.split())[:200]
    head = f"{cmd_label}: "
    body = compress_research_output(raw, max(0, limit - len(head))).strip()
    if not body:
        return None
    return f"{head}{body}"


def read_targets(input_data: dict[str, Any]) -> list[str]:
    """Files this tool call actually read: Read.file_path, Grep/Glob.path,
    NotebookRead.notebook_path, command-position read-style shell, and REPL nested reads."""
    tool = str(input_data.get("tool_name") or "")
    ti = input_data.get("tool_input")
    out: list[str] = []
    if isinstance(ti, dict):
        if tool in STRUCTURED_READ_TOOLS or tool == "view_image":
            out.extend(_paths_from_structured_tool(tool, ti))
        elif is_mcp_tool(tool) and _mcp_tool_is_read_like(tool):
            out.extend(_paths_from_mapping(ti))
        if is_shell_tool(tool):
            out.extend(_bash_read_files(command_from_input(input_data)))
    elif isinstance(ti, str) and is_shell_tool(tool):
        out.extend(_bash_read_files(ti))
    if is_repl_tool(tool):
        repl_reads, _ = repl_nested_activity(input_data)
        out.extend(repl_reads)
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
        if is_shell_tool(tool):
            out.extend(_bash_fetch_urls(command_from_input(input_data)))
    elif isinstance(ti, str) and is_shell_tool(tool):
        out.extend(_bash_fetch_urls(ti))
    if is_repl_tool(tool):
        _, repl_fetches = repl_nested_activity(input_data)
        out.extend(repl_fetches)
    if is_shell_tool(tool):
        cmd = command_from_input(input_data)
        if _explore_script_in_bash_command(cmd):
            # websearch.sh / trace.sh embed source URLs in stdout, not the argv.
            out.extend(_urls_from_any(input_data.get("tool_response")))
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
    """The shell command this tool call executed (normalized), else None."""
    if not is_shell_tool(str(input_data.get("tool_name") or "")):
        return None
    cmd = command_from_input(input_data).strip()
    return cmd or None


def command_from_input(input_data: dict[str, Any]) -> str:
    tool_input = input_data.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "description"):
            value = tool_input.get(key)
            if value:
                return str(value)
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
    if is_shell_tool(tool_name) and MUTATING_BASH_RE.search(command):
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


def format_verifications(records: Any, limit: int = 20) -> list[str]:
    """Render recorded verification_results (from verification_record) into the
    one-line strings the evidence corpus carries. A passing pytest run the agent
    already executed becomes proof the evidence judge can read, so it need not be
    laundered through a research wrapper to be counted."""
    out: list[str] = []
    for r in (records or [])[-limit:]:
        if not isinstance(r, dict):
            continue
        command = str(r.get("command") or "").strip()
        if not command:
            continue
        success = r.get("success")
        status = "PASS" if success is True else "FAIL" if success is False else "RAN"
        summary = str(r.get("summary") or "").strip()
        line = f"{command} -> {status}: {summary}" if summary else f"{command} -> {status}"
        out.append(line)
    return out


def _failure_signature(summary: str) -> str:
    """Normalize a failure summary into a stable class key. Numbers and paths
    differ between occurrences of the same failure, so collapse them so that
    e.g. two 'ECONNREFUSED localhost:5432' land on the same class."""
    s = (summary or "").lower()
    s = re.sub(r"[/\\][^\s]+", " path ", s)  # paths vary
    s = re.sub(r"\d+", "#", s)  # numbers vary
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
