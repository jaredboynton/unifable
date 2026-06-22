"""Transcript rendering and tail helpers for judge prompts.

Transcript tails are budgeted against gpt-realtime-2's 256k-char per-message
limit (see JUDGE_* constants); codex_judge.ask_structured enforces the same cap
at the transport layer.
"""

from __future__ import annotations

import os
import json
import re
from pathlib import Path
from typing import Any

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


def _part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    if part.get("type") == "tool_use":
        name = part.get("name") or "unknown"
        raw_input = part.get("input")
        if isinstance(raw_input, (dict, list)):
            rendered_input = json.dumps(raw_input, ensure_ascii=False, sort_keys=True)
        else:
            rendered_input = str(raw_input or "")
        return f"[tool_use name={name}]\n{rendered_input}"
    if part.get("type") == "tool_result" or part.get("tool_use_id"):
        content = part.get("content")
        if isinstance(content, list):
            pieces = []
            for item in content:
                if isinstance(item, str):
                    pieces.append(item)
                elif isinstance(item, dict):
                    for key in ("text", "content"):
                        if isinstance(item.get(key), str):
                            pieces.append(item[key])
                            break
            rendered = "\n".join(p for p in pieces if p)
        elif isinstance(content, str):
            rendered = content
        else:
            rendered = ""
        return f"[tool_result]\n{rendered}"
    if isinstance(part.get("text"), str):
        return part["text"]
    if isinstance(part.get("content"), str):
        return part["content"]
    return ""


def _record_text(record: dict[str, Any]) -> str:
    content = record.get("message", {}).get("content") if isinstance(record.get("message"), dict) else None
    if content is None:
        content = record.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        rendered = "\n\n".join(part for part in (_part_text(item) for item in content) if part)
        if rendered:
            return rendered
    for key in ("toolUseResult", "lastPrompt", "aiTitle", "summary"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ""


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


def _render_stripped_record(line: str, line_number: int) -> str:
    padded = str(line_number).zfill(6)
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
    body = _record_text(record).strip() or _preview_record(line) or "[no textual content extracted]"
    return "<record " + " ".join(attrs) + ">\n" + body + "\n</record>"


def stripped_transcript(text: str) -> str:
    """Render JSONL transcript records in the stripped line-addressed format.

    This mirrors claudecompact-patcher's default `stripped` renderer: JSONL
    records become XML-like records with line/type/role/timestamp metadata, and
    the body contains extracted message/tool text instead of raw JSON.
    """
    lines = [line for line in re.split(r"\r?\n", text) if line.strip()]
    return "\n".join(_render_stripped_record(line, i + 1) for i, line in enumerate(lines)) + ("\n" if lines else "")


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
