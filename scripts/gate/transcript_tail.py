"""Transcript rendering and tail helpers for judge prompts."""

from __future__ import annotations

import os
import json
import re
from pathlib import Path
from typing import Any

TRANSCRIPT_TOKEN_BUDGET = 50_000


def tail_tokens(text: str, max_tokens: int = TRANSCRIPT_TOKEN_BUDGET) -> str:
    """Return the last `max_tokens` tokens of text.

    Use tiktoken when available for model-token slicing. The hooks have no
    runtime dependency on tiktoken, so fall back to whitespace-delimited token
    spans while preserving the original raw transcript text from the selected
    tail onward.
    """
    if max_tokens <= 0:
        return ""
    try:
        import tiktoken  # type: ignore

        try:
            enc = tiktoken.encoding_for_model(os.environ.get("UNIFABLE_JUDGE_MODEL", "gpt-realtime-2"))
        except Exception:
            enc = tiktoken.get_encoding("o200k_base")
        toks = enc.encode(text)
        if len(toks) <= max_tokens:
            return text
        return enc.decode(toks[-max_tokens:])
    except Exception:
        matches = list(re.finditer(r"\S+", text))
        if len(matches) <= max_tokens:
            return text
        return text[matches[-max_tokens].start():]


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
