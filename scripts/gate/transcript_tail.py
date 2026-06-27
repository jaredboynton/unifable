"""Transcript rendering and tail helpers for judge prompts.

Transcript tails are budgeted against gpt-realtime-2's 256k-char per-message
limit (see JUDGE_* constants); codex_judge.ask_structured enforces the same cap
at the transport layer.

Tool rendering and age-based compression mirror patchpress (tool-use-format +
sentinel-style old-record compression), configurable via UNIFABLE_JUDGE_* env.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from tool_output_compress import compact_tool_output_body, judge_compression_config
    from tool_use_format import (
        compact_formatted_edit,
        format_tool_result,
        format_tool_use,
        is_formatted_edit_text,
    )
except ImportError:  # pragma: no cover
    from scripts.gate.tool_output_compress import compact_tool_output_body, judge_compression_config
    from scripts.gate.tool_use_format import (
        compact_formatted_edit,
        format_tool_result,
        format_tool_use,
        is_formatted_edit_text,
    )

TRANSCRIPT_TOKEN_BUDGET = 50_000
# Conservative chars-per-token bound. Real model tokens average ~4 chars, but a
# whitespace-delimited span in a transcript (JSON blobs, base64, code, long IDs)
# can be hundreds of chars, so counting spans as tokens lets the "tail" balloon
# past the model's input-char limit when tiktoken is absent. The char ceiling
# bounds EVERY path so the judge prompt can never exceed that limit.
MAX_CHARS_PER_TOKEN = 4

# gpt-realtime-2 hard per-message char limit (single message field).
JUDGE_MAX_MESSAGE_CHARS = 256_000
JUDGE_MESSAGE_SAFETY_MARGIN = 4_000
JUDGE_EFFECTIVE_MAX_CHARS = JUDGE_MAX_MESSAGE_CHARS - JUDGE_MESSAGE_SAFETY_MARGIN
# Reserve for user wrappers (disarm labels, goal, claim, "QUESTION: " prefix).
JUDGE_USER_WRAPPER_RESERVE = 16_000
JUDGE_TRANSCRIPT_CHAR_BUDGET = JUDGE_EFFECTIVE_MAX_CHARS - JUDGE_USER_WRAPPER_RESERVE

_TRUNC_MARKER = "\n...[truncated {n} chars]"

# Retention-ratio truncation (OpenAI Realtime cost guide): when a judge transcript
# exceeds budget, drop the oldest content in LARGE chunks instead of sliding by one
# unit per call. The window start is "sticky" -- it only advances when accumulated
# growth crosses a chunk boundary -- so consecutive same-session judge calls share a
# byte-identical, append-only prefix that gpt-realtime-2 can cache, instead of busting
# the cache near the beginning every turn. retention_ratio=1.0 reproduces plain
# last-N tailing (no extra drop). Override with UNIFABLE_JUDGE_RETENTION_RATIO.
DEFAULT_RETENTION_RATIO = 0.8


def _retention_ratio() -> float:
    try:
        ratio = float(os.environ.get("UNIFABLE_JUDGE_RETENTION_RATIO") or DEFAULT_RETENTION_RATIO)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_RATIO
    if ratio <= 0.0:
        return DEFAULT_RETENTION_RATIO
    return min(ratio, 1.0)


def _sticky_start(n: int, budget: int, ratio: float) -> int:
    """First index of a sticky retention window over `n` units bounded by `budget`."""
    if budget <= 0:
        return n
    if n <= budget:
        return 0
    if ratio >= 1.0:
        return n - budget
    drop_chunk = max(1, int(round(budget * (1.0 - ratio))))
    overflow = n - budget
    steps = (overflow + drop_chunk - 1) // drop_chunk  # ceil
    start = steps * drop_chunk
    if start >= n:
        start = n - budget
    return start


def retention_window(text: str, budget_chars: int, retention_ratio: float | None = None) -> str:
    """Tail of `text` bounded by `budget_chars`, cut on a sticky chunk boundary.

    Unlike `text[-budget_chars:]` (which shifts the window start by one char per
    appended char and busts prompt-cache prefixes), the start here only jumps in
    `(1 - retention_ratio)` chunks, so the retained prefix stays byte-identical
    across calls until the next chunk drop. Always returns a suffix of `text`.
    """
    s = str(text or "")
    n = len(s)
    if budget_chars <= 0:
        return ""
    if n <= budget_chars:
        return s
    ratio = _retention_ratio() if retention_ratio is None else retention_ratio
    start = _sticky_start(n, budget_chars, ratio)
    return s[start:]


def cap_judge_message(text: str, max_chars: int = JUDGE_EFFECTIVE_MAX_CHARS) -> str:
    """Tail-preserving truncation so judge payloads never exceed the API char limit."""
    if max_chars <= 0:
        return ""
    s = str(text or "")
    if len(s) <= max_chars:
        return s
    dropped = len(s) - max_chars
    marker = _TRUNC_MARKER.format(n=dropped)
    keep = max(0, max_chars - len(marker))
    return s[-keep:] + marker if keep else marker[-max_chars:]


def fit_judge_user_message(
    prefix: str,
    body: str,
    *,
    suffix: str = "",
    max_chars: int = JUDGE_EFFECTIVE_MAX_CHARS,
) -> str:
    """Build prefix+body+suffix, trimming body from the front when over max_chars."""
    prefix = str(prefix or "")
    body = str(body or "")
    suffix = str(suffix or "")
    fixed = len(prefix) + len(suffix)
    if fixed >= max_chars:
        return cap_judge_message(prefix + suffix, max_chars)
    room = max_chars - fixed
    return prefix + cap_judge_message(body, room) + suffix


def tail_tokens(text: str, max_tokens: int = TRANSCRIPT_TOKEN_BUDGET) -> str:
    """Return the last `max_tokens` tokens of text, hard-bounded by characters.

    Uses tiktoken for model-token slicing when available. The hooks have no
    runtime dependency on tiktoken, so without it the tail is taken purely by
    character count (`max_tokens * MAX_CHARS_PER_TOKEN`) -- a closer token
    approximation for dense text than whitespace spans, and one that cannot
    overflow the model input limit. The char ceiling is applied on the tiktoken
    path too, as a backstop against encodings with a high chars-per-token ratio.
    """
    if max_tokens <= 0:
        return ""
    char_cap = min(max_tokens * MAX_CHARS_PER_TOKEN, JUDGE_TRANSCRIPT_CHAR_BUDGET)
    out = text
    try:
        import tiktoken  # type: ignore

        try:
            enc = tiktoken.encoding_for_model(os.environ.get("UNIFABLE_JUDGE_MODEL", "gpt-realtime-2"))
        except Exception:
            enc = tiktoken.get_encoding("o200k_base")
        toks = enc.encode(text)
        if len(toks) > max_tokens:
            out = enc.decode(toks[-max_tokens:])
    except Exception:
        out = text  # the char ceiling below does the bounding
    if len(out) > char_cap:
        out = out[-char_cap:]
    return out


def _attr(value: Any) -> str:
    return str(value).replace('"', "'").replace("\n", " ")


def _tool_format_meta(entry: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "lineNumber": entry.get("lineNumber") if entry else None,
        "recordHash": entry.get("hash") if entry else None,
        "cwdPrefix": os.environ.get("UNIFABLE_JUDGE_TRANSCRIPT_CWD_PREFIX") or None,
    }


def _render_part_for_prompt(part: Any, meta: dict[str, Any]) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    if part.get("type") == "tool_use":
        return format_tool_use(part, meta)
    if part.get("type") == "tool_result" or part.get("tool_use_id"):
        return format_tool_result(part, meta)
    if isinstance(part.get("text"), str):
        return part["text"]
    if isinstance(part.get("content"), str):
        return part["content"]
    return ""


def _codex_payload_text(record: dict[str, Any]) -> str:
    """Codex rollout records nest text under top-level ``payload``
    (response_item / event_msg / turn_context), not the Claude ``message`` shape.

    Mirrors the JS ``codexPayloadText`` in unifusion's compact-full-transcript.mjs
    so both renderers expose the same text; precedence is
    ``message`` -> ``text`` -> ``content[].text`` -> ``result.Ok.content`` -> ``output``.
    """
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""
    msg = payload.get("message")
    if isinstance(msg, str) and msg:
        return msg
    text = payload.get("text")
    if isinstance(text, str) and text:
        return text
    content = payload.get("content")
    if isinstance(content, list):
        parts = [
            c.get("text")
            for c in content
            if isinstance(c, dict) and isinstance(c.get("text"), str) and c.get("text")
        ]
        if parts:
            return "\n".join(parts)
    result = payload.get("result")
    ok = result.get("Ok") if isinstance(result, dict) else None
    ok_content = ok.get("content") if isinstance(ok, dict) else None
    if isinstance(ok_content, list):
        parts = []
        for c in ok_content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
        parts = [p for p in parts if p]
        if parts:
            return "\n".join(parts)
    output = payload.get("output")
    if isinstance(output, str) and output:
        return output
    return ""


def _record_text(record: dict[str, Any], entry: dict[str, Any] | None = None) -> str:
    meta = _tool_format_meta(entry)
    content = record.get("message", {}).get("content") if isinstance(record.get("message"), dict) else None
    if content is None:
        content = record.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        rendered = "\n\n".join(part for part in (_render_part_for_prompt(item, meta) for item in content) if part)
        if rendered:
            return rendered
    for key in ("toolUseResult", "lastPrompt", "aiTitle", "summary"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
    cx = _codex_payload_text(record)
    if cx:
        return cx
    return ""


def _is_tool_output_record(record: dict[str, Any]) -> bool:
    if record.get("toolUseResult") or record.get("sourceToolAssistantUUID"):
        return True
    content = record.get("message", {}).get("content") if isinstance(record.get("message"), dict) else None
    if content is None:
        content = record.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(part, dict) and (part.get("type") == "tool_result" or part.get("tool_use_id")) for part in content)


def _is_tool_use_record(record: dict[str, Any]) -> bool:
    msg = record.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "tool_use" for part in content
    )


def _apply_record_compression(body: str, record: dict[str, Any], entry: dict[str, Any], record_count: int) -> str:
    cfg = judge_compression_config()
    keep_recent = int(cfg["keep_recent"])
    if keep_recent <= 0:
        return body
    line_number = int(entry.get("lineNumber") or 0)
    if line_number > record_count - keep_recent:
        return body
    if _is_tool_output_record(record):
        compacted = compact_tool_output_body(body, entry, cfg)
        return compacted["body"]
    if _is_tool_use_record(record) and is_formatted_edit_text(body):
        compacted = compact_formatted_edit(
            body,
            entry,
            min_chars=int(cfg["tool_use_min_chars"]),
            head_chars=int(cfg["tool_use_head_chars"]),
            tail_chars=int(cfg["tool_use_tail_chars"]),
        )
        return compacted["body"]
    return body


def _preview_record(line: str) -> str:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return re.sub(r"\s+", " ", line)[:160]
    if not isinstance(record, dict):
        return re.sub(r"\s+", " ", line)[:160]
    pieces = []
    if record.get("type"):
        pieces.append("type=" + str(record["type"]))
    if record.get("uuid"):
        pieces.append("uuid=" + str(record["uuid"]))
    msg = record.get("message")
    if isinstance(msg, dict) and msg.get("role"):
        pieces.append("role=" + str(msg["role"]))
    text = _record_text(record)
    if text:
        pieces.append("text=" + re.sub(r"\s+", " ", text)[:160])
    return " | ".join(pieces)


def _build_entries(raw_lines: list[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for idx, line in enumerate(raw_lines):
        entries.append(
            {
                "lineNumber": idx + 1,
                "raw": line,
                "hash": hashlib.sha256(line.encode("utf-8", "replace")).hexdigest(),
                "preview": _preview_record(line),
            }
        )
    return entries


def _render_stripped_record(entry: dict[str, Any], record_count: int) -> str:
    line = entry["raw"]
    padded = str(entry["lineNumber"]).zfill(6)
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return f'<record line="{padded}" kind="unparsed">\n{line}\n</record>'
    if not isinstance(record, dict):
        return f'<record line="{padded}" kind="unparsed">\n{line}\n</record>'
    attrs = [
        f'line="{padded}"',
        f'type="{_attr(record.get("type") or "unknown")}"',
    ]
    msg = record.get("message")
    if isinstance(msg, dict) and msg.get("role"):
        attrs.append(f'role="{_attr(msg["role"])}"')
    if record.get("timestamp"):
        attrs.append(f'timestamp="{_attr(record["timestamp"])}"')
    body = _record_text(record, entry).strip() or entry.get("preview") or "[no textual content extracted]"
    body = _apply_record_compression(body, record, entry, record_count)
    return "<record " + " ".join(attrs) + ">\n" + body + "\n</record>"


def stripped_transcript(text: str) -> str:
    """Render JSONL transcript records in the stripped line-addressed format.

    Mirrors patchpress stripped renderer with patchpress tool-use formatting and
    age-based compression on old tool outputs / formatted edits.
    """
    lines = [line for line in re.split(r"\r?\n", text) if line.strip()]
    if not lines:
        return ""
    entries = _build_entries(lines)
    record_count = len(entries)
    return (
        "\n".join(_render_stripped_record(entry, record_count) for entry in entries) + "\n"
    )


def stripped_transcript_tail(path: str | os.PathLike[str] | None, max_tokens: int = TRANSCRIPT_TOKEN_BUDGET) -> str:
    """Read a transcript file, strip JSONL framing, and return its last tokens."""
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return tail_tokens(stripped_transcript(raw), max_tokens)


def _is_human_user_turn(record: dict[str, Any]) -> str:
    """Return the human prompt text when *record* is a genuine user turn, else "".

    A transcript carries two kinds of ``role:user`` records: the human's typed
    prompt (``content`` is a string, or a list of text/non-tool blocks) and the
    host's tool-result turns (``content`` is a list containing ``tool_result``
    blocks). Only the former marks a task boundary, so tool-result turns are
    rejected: they recur many times within one task and would make the lineage
    fingerprint unstable."""
    if not isinstance(record, dict):
        return ""
    msg = record.get("message") if isinstance(record.get("message"), dict) else record
    role = msg.get("role") or record.get("role")
    if role != "user":
        return ""
    content = msg.get("content")
    if content is None:
        content = record.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        # Reject any turn that carries a tool_result block -- that is a host turn.
        for item in content:
            if isinstance(item, dict) and (item.get("type") == "tool_result" or item.get("tool_use_id")):
                return ""
        texts = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                texts.append(item["text"])
        return "\n".join(t for t in texts if t).strip()
    return ""


def latest_user_prompt_fingerprint(path: str | os.PathLike[str] | None) -> str:
    """Stable 16-hex fingerprint of the latest HUMAN user prompt in a transcript.

    Used as the breaker's task-lineage fallback when the ledger's per-prompt
    ``active_task`` is empty (the common case in production -- e.g. after a
    /compact, gate_prompt has not re-pinned it). The fingerprint is derived from
    the most recent genuine human prompt (see ``_is_human_user_turn``), which is
    constant for the duration of that task and distinct across tasks, so two
    different prompts in one session get distinct breaker keys without re-judging
    within a task. Returns "" when no transcript or no human turn is found, so the
    caller falls back to the empty component (no regression vs today)."""
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _is_human_user_turn(record)
        if text:
            return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
    return ""


def stripped_transcript_retained(
    path: str | os.PathLike[str] | None,
    max_tokens: int = TRANSCRIPT_TOKEN_BUDGET,
    retention_ratio: float | None = None,
) -> str:
    """Read a transcript file, strip JSONL framing, and bound it with a sticky
    retention window instead of a sliding `tail_tokens` tail.

    `tail_tokens` keeps the last `max_tokens*MAX_CHARS_PER_TOKEN` chars (a suffix
    that slides one char per appended char), so a transcript fed to a same-session
    judge over consecutive turns has a different prefix every turn and busts
    gpt-realtime-2's prompt cache. `retention_window` runs over the FULL
    append-only transcript and only advances its window start in chunks, so the
    retained prefix stays byte-identical across appends until the next chunk drop
    -- the cache-stable variant for the requirement-validation judge path. The
    char budget matches `tail_tokens` so payloads can never exceed the model's
    input-char limit.
    """
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    char_budget = min(max(max_tokens, 0) * MAX_CHARS_PER_TOKEN, JUDGE_TRANSCRIPT_CHAR_BUDGET)
    return retention_window(stripped_transcript(raw), char_budget, retention_ratio)
