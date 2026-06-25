#!/usr/bin/env python3
"""Render trace JSON into a hydrated markdown report.

The trace prompt asks the model to return only a JSON object with
opening_summary and code_passages. This renderer turns that into markdown and
reads cited source bytes directly from disk. If the input is not structured
JSON or wire format, the text passes through unchanged.
"""
import json
import os
import re
import sys

BT = chr(96)
FENCE = BT * 3
MAX_SPAN = 400

LANG_BY_EXT = {
    ".bash": "bash",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "jsx",
    ".md": "markdown",
    ".py": "python",
    ".rs": "rust",
    ".sh": "bash",
    ".sql": "sql",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
}


def read_file(repo, rel):
    file_path = os.path.join(repo, rel)
    try:
        with open(file_path, errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def safe_rel_path(repo, rel):
    if not isinstance(rel, str):
        return None
    rel = rel.strip()
    if not rel or os.path.isabs(rel):
        return None
    norm = os.path.normpath(rel)
    if norm == "." or norm.startswith("..") or os.path.isabs(norm):
        return None
    abs_path = os.path.abspath(os.path.join(repo, norm))
    root = os.path.abspath(repo)
    try:
        common = os.path.commonpath([root, abs_path])
    except ValueError:
        return None
    if common != root:
        return None
    return norm


def language_for(file_path):
    _, ext = os.path.splitext(file_path)
    return LANG_BY_EXT.get(ext.lower(), "")


def fence_for(code):
    longest = 0
    for match in re.finditer(re.escape(BT) + "+", code):
        longest = max(longest, len(match.group(0)))
    return BT * max(3, longest + 1)


def extract_json_object(text):
    stripped = text.strip()
    if stripped.startswith(FENCE):
        stripped = re.sub(r"^\s*" + re.escape(FENCE) + r"(?:json)?\s*", "", stripped, count=1)
        stripped = re.sub(r"\s*" + re.escape(FENCE) + r"\s*$", "", stripped, count=1)

    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "opening_summary" in value and "code_passages" in value:
            return value
    return None


def number_value(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def passage_field(passage, *names):
    for name in names:
        if name in passage:
            return passage[name]
    return None


def render_trace_json(repo, data):
    summary = str(data.get("opening_summary", "")).strip()
    passages = data.get("code_passages")
    if not isinstance(passages, list):
        passages = []

    output = ["## Trace", summary or "No opening summary returned.", "", "## Code passages"]

    for index, passage in enumerate(passages, start=1):
        if not isinstance(passage, dict):
            continue
        rel = safe_rel_path(repo, passage_field(passage, "file_path", "path", "file"))
        start = number_value(passage_field(passage, "start_line", "start", "startLine"))
        end = number_value(passage_field(passage, "end_line", "end", "endLine"))
        ref = f"<ref{index}>"

        if not rel or not start or not end or start < 1 or end < start or end - start + 1 > MAX_SPAN:
            output.extend(["", f"{ref} invalid passage: {json.dumps(passage, sort_keys=True)}"])
            continue

        lines = read_file(repo, rel)
        if not lines:
            output.extend(["", f"{ref} could not read {BT}{rel}:{start}-{end}{BT}"])
            continue

        end = min(end, len(lines))
        start = min(start, end)
        code = "\n".join(lines[start - 1:end])
        fence = fence_for(code)
        lang = language_for(rel)
        output.extend([
            "",
            f"{ref} {BT}{rel}:{start}-{end}{BT}",
            f"{fence}{lang}",
            code,
            fence,
        ])

    return "\n".join(output).rstrip() + "\n"


def render(repo, text):
    data = extract_json_object(text)
    if data is not None:
        return render_trace_json(repo, data)
    if re.search(r"^SECTION\s+[A-Za-z]", text, re.M) or "<file:" in text:
        return render_wire(repo, text, "trace")
    if re.search(r"^SECTION\s+[A-Za-z]", text, re.M) and ("<url:" in text or "<quote:" in text):
        return render_wire(repo, text, "websearch")
    return text


def render_wire(repo, text, mode):
    import subprocess

    script_dir = os.path.dirname(os.path.abspath(__file__))
    rehydrate = os.path.join(script_dir, "lib", "rehydrate-explore-wire.mjs")
    cmd = ["node", rehydrate, "--mode", mode]
    if mode == "trace":
        cmd.extend(["--workspace", repo])
    proc = subprocess.run(cmd, input=text, capture_output=True, text=True)
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout
    return text


def main():
    if len(sys.argv) < 2:
        print("usage: expand_citations.py <repo_root> < trace-output", file=sys.stderr)
        sys.exit(2)
    sys.stdout.write(render(sys.argv[1], sys.stdin.read()))


if __name__ == "__main__":
    main()
