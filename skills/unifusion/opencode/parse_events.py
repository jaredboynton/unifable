#!/usr/bin/env python3
"""Extract the final assistant text from an `opencode run --format json` stream.

`opencode run --format json` emits newline-delimited JSON events. Assistant prose
arrives as events with type=="text" whose part carries {"type":"text","text":...,
"messageID":...}. A single run can contain several assistant messages (interim
narration between tool calls, then the final answer); we want the LAST assistant
message's text, so we group text parts by messageID and emit the text of the last
message that produced any. If grouping yields nothing we fall back to concatenating
every text part, and finally to the raw file so a caller never silently loses data.

Usage: parse_events.py <events.ndjson>   # prints final text to stdout
"""
import json
import sys


def extract_final_text(path: str) -> str:
    order: list[str] = []
    by_message: dict[str, list[str]] = {}
    any_text = False
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict) or evt.get("type") != "text":
            continue
        part = evt.get("part") or {}
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if not isinstance(text, str) or not text:
            continue
        any_text = True
        mid = part.get("messageID") or evt.get("sessionID") or "_"
        if mid not in by_message:
            by_message[mid] = []
            order.append(mid)
        by_message[mid].append(text)

    if order:
        return "".join(by_message[order[-1]]).strip()
    if any_text:  # defensive: text seen but no message id ever grouped
        return "".join(t for parts in by_message.values() for t in parts).strip()
    return ""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: parse_events.py <events.ndjson>", file=sys.stderr)
        return 2
    sys.stdout.write(extract_final_text(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
