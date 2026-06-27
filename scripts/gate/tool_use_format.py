"""Patchpress-compatible tool-use rendering for judge transcript tails.

Ports the deterministic logic from patchpress scripts/tool-use-format.mjs.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

DEFAULT_CONTEXT_LINES = 3
SMALL_EDIT_CHAR_THRESHOLD = 400
WRITE_HEAD_LINES = 40
WRITE_TAIL_LINES = 10

EDIT_TOOL_NAMES = frozenset({"Edit", "StrReplace", "MultiEdit", "NotebookEdit"})


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


def normalize_path(file_path: str, cwd_prefix: str | None) -> dict[str, str]:
    raw = str(file_path or "").strip()
    if not raw:
        return {"display": "(unknown)", "absolute": raw}
    prefix = str(cwd_prefix or "")
    if prefix and raw.startswith(prefix):
        relative = raw[len(prefix) :].lstrip("/")
        return {"display": relative or raw, "absolute": raw}
    return {"display": raw, "absolute": raw}


def normalize_edit_input(input_obj: Any) -> dict[str, Any]:
    if not isinstance(input_obj, dict):
        return {"filePath": "", "oldText": "", "newText": "", "edits": None}
    file_path = input_obj.get("file_path") or input_obj.get("path") or input_obj.get("filePath") or ""
    old_text = (
        input_obj.get("old_str")
        if "old_str" in input_obj
        else input_obj.get("old_string", input_obj.get("oldString", ""))
    )
    new_text = (
        input_obj.get("new_str")
        if "new_str" in input_obj
        else input_obj.get("new_string", input_obj.get("newString", ""))
    )
    edits_raw = input_obj.get("edits")
    edits = None
    if isinstance(edits_raw, list):
        edits = []
        for edit in edits_raw:
            if not isinstance(edit, dict):
                continue
            edits.append(
                {
                    "oldText": edit.get("old_str", edit.get("old_string", edit.get("oldString", ""))),
                    "newText": edit.get("new_str", edit.get("new_string", edit.get("newString", ""))),
                }
            )
    return {
        "filePath": file_path,
        "oldText": str(old_text),
        "newText": str(new_text),
        "edits": edits,
    }


def _split_lines(text: str) -> list[str]:
    return re.sub(r"\r\n?", "\n", str(text or "")).split("\n")


def _trim_common_prefix_suffix(old_lines: list[str], new_lines: list[str]) -> dict[str, list[str]]:
    start = 0
    while start < len(old_lines) and start < len(new_lines) and old_lines[start] == new_lines[start]:
        start += 1
    old_end = len(old_lines) - 1
    new_end = len(new_lines) - 1
    while old_end >= start and new_end >= start and old_lines[old_end] == new_lines[new_end]:
        old_end -= 1
        new_end -= 1
    return {
        "prefix": old_lines[:start],
        "oldMiddle": old_lines[start : old_end + 1],
        "newMiddle": new_lines[start : new_end + 1],
        "suffix": old_lines[old_end + 1 :],
    }


def _lcs_pairs(old_lines: list[str], new_lines: list[str]) -> list[dict[str, Any]]:
    m, n = len(old_lines), len(new_lines)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            if old_lines[i] == new_lines[j]:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    pairs: list[dict[str, Any]] = []
    i = j = 0
    while i < m and j < n:
        if old_lines[i] == new_lines[j]:
            pairs.append({"type": "same", "line": old_lines[i]})
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            pairs.append({"type": "remove", "line": old_lines[i]})
            i += 1
        else:
            pairs.append({"type": "add", "line": new_lines[j]})
            j += 1
    while i < m:
        pairs.append({"type": "remove", "line": old_lines[i]})
        i += 1
    while j < n:
        pairs.append({"type": "add", "line": new_lines[j]})
        j += 1
    return pairs


def _collapse_unchanged_runs(pairs: list[dict[str, Any]], context_lines: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    unchanged_run: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal unchanged_run
        if not unchanged_run:
            return
        if len(unchanged_run) <= context_lines * 2:
            out.extend({"type": "context", "line": item["line"]} for item in unchanged_run)
        else:
            out.extend({"type": "context", "line": item["line"]} for item in unchanged_run[:context_lines])
            out.append({"type": "elide", "count": len(unchanged_run) - context_lines * 2})
            out.extend({"type": "context", "line": item["line"]} for item in unchanged_run[-context_lines:])
        unchanged_run = []

    for pair in pairs:
        if pair["type"] == "same":
            unchanged_run.append(pair)
            continue
        flush()
        out.append(pair)
    flush()
    return out


def line_diff(old_text: str, new_text: str, *, context_lines: int = DEFAULT_CONTEXT_LINES) -> list[dict[str, Any]]:
    old_lines = _split_lines(old_text)
    new_lines = _split_lines(new_text)
    trimmed = _trim_common_prefix_suffix(old_lines, new_lines)
    pairs: list[dict[str, Any]] = [{"type": "context", "line": line} for line in trimmed["prefix"]]
    pairs.extend(
        _collapse_unchanged_runs(_lcs_pairs(trimmed["oldMiddle"], trimmed["newMiddle"]), context_lines)
    )
    pairs.extend({"type": "context", "line": line} for line in trimmed["suffix"])
    return pairs


def _render_diff_pairs(pairs: list[dict[str, Any]], file_path: str) -> dict[str, Any]:
    lines: list[str] = []
    if file_path:
        lines.extend([f"--- a/{file_path}", f"+++ b/{file_path}"])
    added = removed = 0
    for pair in pairs:
        ptype = pair["type"]
        if ptype == "context":
            lines.append(" " + pair["line"])
        elif ptype == "remove":
            lines.append("-" + pair["line"])
            removed += 1
        elif ptype == "add":
            lines.append("+" + pair["line"])
            added += 1
        elif ptype == "elide":
            lines.append(f"[... {pair['count']} unchanged lines ...]")
    return {"body": "\n".join(lines), "added": added, "removed": removed}


def _tool_header(name: str, meta: dict[str, Any]) -> str:
    parts = [f"@@tool {name or 'unknown'}"]
    line_number = meta.get("lineNumber")
    if line_number is not None:
        parts.append(f"line={int(line_number):06d}")
    record_hash = meta.get("recordHash")
    if record_hash:
        parts.append(f"sha256={record_hash}")
    return " ".join(parts)


def _stats_footer(*, added: int, removed: int, input_sha256: str | None = None) -> str:
    bits = [f"stats: +{added} -{removed} lines"]
    if input_sha256:
        bits.append(f"input_sha256={input_sha256}")
    return " | ".join(bits)


def format_edit_diff(
    old_text: str,
    new_text: str,
    file_path: str,
    meta: dict[str, Any] | None = None,
    *,
    small_edit_threshold: int = SMALL_EDIT_CHAR_THRESHOLD,
    context_lines: int = DEFAULT_CONTEXT_LINES,
) -> str:
    meta = meta or {}
    paths = normalize_path(file_path, meta.get("cwdPrefix"))
    input_sha256 = sha256_text(json.dumps({"filePath": file_path, "oldText": old_text, "newText": new_text}))
    combined = len(old_text) + len(new_text)
    lines = [_tool_header(meta.get("toolName") or "Edit", meta), f"@@file {paths['display']}"]
    if paths["absolute"] and paths["absolute"] != paths["display"]:
        lines.append(f"@@file_abs {paths['absolute']}")
    added = removed = 0
    if combined <= small_edit_threshold:
        for line in _split_lines(old_text):
            lines.append("-" + line)
            removed += 1
        for line in _split_lines(new_text):
            lines.append("+" + line)
            added += 1
    else:
        rendered = _render_diff_pairs(line_diff(old_text, new_text, context_lines=context_lines), paths["display"])
        lines.append(rendered["body"])
        added = rendered["added"]
        removed = rendered["removed"]
    lines.append(_stats_footer(added=added, removed=removed, input_sha256=input_sha256))
    return "\n".join(lines)


def format_edit_tool(
    name: str,
    input_obj: Any,
    meta: dict[str, Any] | None = None,
    **options: Any,
) -> str:
    meta = meta or {}
    normalized = normalize_edit_input(input_obj)
    edits = normalized.get("edits")
    if edits:
        return "\n\n".join(
            format_edit_diff(
                edit["oldText"],
                edit["newText"],
                normalized["filePath"],
                {**meta, "toolName": f"{name}[{idx}]"},
                **options,
            )
            for idx, edit in enumerate(edits)
        )
    return format_edit_diff(
        normalized["oldText"],
        normalized["newText"],
        normalized["filePath"],
        {**meta, "toolName": name},
        **options,
    )


def format_write_tool(input_obj: Any, meta: dict[str, Any] | None = None, **options: Any) -> str:
    meta = meta or {}
    if not isinstance(input_obj, dict):
        input_obj = {}
    file_path = input_obj.get("file_path") or input_obj.get("path") or input_obj.get("filePath") or ""
    contents = str(input_obj.get("contents", input_obj.get("content", "")))
    paths = normalize_path(file_path, meta.get("cwdPrefix"))
    input_sha256 = sha256_text(json.dumps({"filePath": file_path, "contents": contents}))
    lines = [_tool_header(meta.get("toolName") or "Write", meta), f"@@file {paths['display']}"]
    if paths["absolute"] and paths["absolute"] != paths["display"]:
        lines.append(f"@@file_abs {paths['absolute']}")
    content_lines = _split_lines(contents)
    head_limit = int(options.get("writeHeadLines", WRITE_HEAD_LINES))
    tail_limit = int(options.get("writeTailLines", WRITE_TAIL_LINES))
    if len(content_lines) <= head_limit + tail_limit:
        lines.extend(["```", contents.rstrip("\n"), "```"])
    else:
        head = "\n".join(content_lines[:head_limit])
        tail = "\n".join(content_lines[-tail_limit:])
        omitted = max(len(content_lines) - head_limit - tail_limit, 0)
        lines.extend(["```", head, f"[... {omitted} lines omitted ...]", tail, "```"])
    lines.append(f"stats: lines={len(content_lines)} | input_sha256={input_sha256}")
    return "\n".join(lines)


def format_apply_patch(input_obj: Any, meta: dict[str, Any] | None = None) -> str:
    meta = meta or {}
    if not isinstance(input_obj, dict):
        input_obj = {}
    patch = str(input_obj.get("patch", input_obj.get("input", "")))
    input_sha256 = sha256_text(patch)
    lines = [
        _tool_header(meta.get("toolName") or "apply_patch", meta),
        "```diff",
        patch.rstrip("\n"),
        "```",
        f"stats: input_sha256={input_sha256}",
    ]
    return "\n".join(lines)


def format_diff_lines_result(obj: dict[str, Any], meta: dict[str, Any] | None = None, **options: Any) -> str:
    meta = meta or {}
    file_path = obj.get("file_path") or obj.get("path") or ""
    diff_lines = obj.get("diffLines") if isinstance(obj.get("diffLines"), list) else []
    paths = normalize_path(file_path, meta.get("cwdPrefix"))
    lines = [_tool_header(meta.get("toolName") or "EditResult", meta), f"@@file {paths['display']}"]
    if paths["absolute"] and paths["absolute"] != paths["display"]:
        lines.append(f"@@file_abs {paths['absolute']}")
    lines.extend([f"--- a/{paths['display']}", f"+++ b/{paths['display']}"])
    added = removed = 0
    unchanged_run: list[dict[str, str]] = []
    context_lines = int(options.get("contextLines", DEFAULT_CONTEXT_LINES))

    def flush() -> None:
        nonlocal unchanged_run
        if not unchanged_run:
            return
        if len(unchanged_run) <= context_lines * 2:
            lines.extend(" " + item["content"] for item in unchanged_run)
        else:
            lines.extend(" " + item["content"] for item in unchanged_run[:context_lines])
            lines.append(f"[... {len(unchanged_run) - context_lines * 2} unchanged lines ...]")
            lines.extend(" " + item["content"] for item in unchanged_run[-context_lines:])
        unchanged_run = []

    for item in diff_lines:
        if not isinstance(item, dict):
            continue
        dtype = item.get("type") or "unchanged"
        content = str(item.get("content", ""))
        if dtype == "unchanged":
            unchanged_run.append({"content": content})
            continue
        flush()
        if dtype == "removed":
            lines.append("-" + content)
            removed += 1
        elif dtype == "added":
            lines.append("+" + content)
            added += 1
        else:
            lines.append(" " + content)
    flush()
    input_sha256 = sha256_text(json.dumps(obj, sort_keys=True))
    lines.append(_stats_footer(added=added, removed=removed, input_sha256=input_sha256))
    return "\n".join(lines)


def _try_parse_json_object(text: str) -> dict[str, Any] | None:
    trimmed = str(text or "").strip()
    if not trimmed.startswith("{"):
        return None
    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def format_tool_use(part: dict[str, Any], meta: dict[str, Any] | None = None, **options: Any) -> str:
    meta = meta or {}
    name = part.get("name") or "unknown"
    input_obj = part.get("input") if isinstance(part.get("input"), (dict, list)) else {}
    if name in EDIT_TOOL_NAMES:
        return format_edit_tool(name, input_obj, meta, **options)
    if name == "Write":
        return format_write_tool(input_obj, meta, **options)
    if name == "apply_patch":
        return format_apply_patch(input_obj, meta)
    input_sha256 = sha256_text(json.dumps(input_obj, sort_keys=True))
    return "\n".join(
        [
            _tool_header(name, meta),
            json.dumps(input_obj, indent=2, ensure_ascii=False, sort_keys=True),
            f"stats: input_sha256={input_sha256}",
        ]
    )


def format_tool_result_content(content: Any, meta: dict[str, Any] | None = None, **options: Any) -> str:
    meta = meta or {}
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    pieces.append(item["text"])
                elif isinstance(item.get("content"), str):
                    pieces.append(item["content"])
        text = "\n".join(p for p in pieces if p)
    else:
        text = ""
    parsed = _try_parse_json_object(text)
    if parsed and isinstance(parsed.get("diffLines"), list) and (parsed.get("file_path") or parsed.get("path")):
        return format_diff_lines_result(parsed, {**meta, "toolName": "EditResult"}, **options)
    return text


def format_tool_result(part: dict[str, Any], meta: dict[str, Any] | None = None, **options: Any) -> str:
    formatted = format_tool_result_content(part.get("content"), meta, **options)
    if not formatted:
        return "[tool_result]"
    return "[tool_result]\n" + formatted


def is_formatted_edit_text(text: str) -> bool:
    return str(text or "").lstrip().startswith("@@tool ")


def compact_formatted_edit(
    text: str,
    entry: dict[str, Any],
    *,
    min_chars: int = 800,
    head_chars: int = 400,
    tail_chars: int = 200,
) -> dict[str, Any]:
    body = str(text or "")
    if len(body) <= min_chars:
        return {"body": body, "compressed": False}
    file_match = re.match(r"^@@tool[^\n]*\n@@file ([^\n]+)\n([\s\S]*)$", body, re.MULTILINE)
    if not file_match:
        omitted = max(len(body) - head_chars, 0)
        return {
            "body": (
                body[:head_chars]
                + f"\n\n[edit compressed: original_chars={len(body)} omitted_chars={omitted} "
                f"line={entry.get('lineNumber')} body_sha256={sha256_text(body)} "
                f"record_sha256={entry.get('hash')}]\n"
            ),
            "compressed": True,
            "originalChars": len(body),
            "omittedChars": omitted,
        }
    if re.search(r"(-[^\n]+\n)(\+[^\n]+\n)+", body):
        return {"body": body, "compressed": False}
    recompressed = re.sub(
        r"\[\.\.\. (\d+) unchanged lines \.\.\.\]",
        lambda m: f"[... {m.group(1)} unchanged lines (compressed) ...]",
        body,
    )
    if len(recompressed) >= len(body):
        head = body[:head_chars]
        tail = body[max(len(body) - tail_chars, head_chars) :]
        omitted = max(len(body) - len(head) - len(tail), 0)
        return {
            "body": "\n".join(
                [
                    head,
                    "",
                    f"[edit compressed: original_chars={len(body)} omitted_chars={omitted} "
                    f"line={entry.get('lineNumber')} body_sha256={sha256_text(body)} "
                    f"record_sha256={entry.get('hash')}]",
                    "",
                    tail,
                ]
            ),
            "compressed": True,
            "originalChars": len(body),
            "omittedChars": omitted,
        }
    return {
        "body": recompressed,
        "compressed": len(recompressed) < len(body),
        "originalChars": len(body),
        "omittedChars": max(len(body) - len(recompressed), 0),
    }
