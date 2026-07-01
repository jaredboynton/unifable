#!/usr/bin/env python3
"""Extract the final assistant text from an `opencode run --format json` stream.

`opencode run --format json` emits newline-delimited JSON events. Assistant prose
arrives as events with type=="text" whose part carries {"type":"text","text":...}.
A run has one or more steps (each delimited by a `step_start` event): interim
narration and tool calls happen in earlier steps, and the final answer is the text
of the last step.

Capture strategy, in order of preference:
  1. All text parts at/after the last `step_start` (the final turn's answer). This
     drops interim "I'll research..." narration from earlier steps.
  2. If the final turn produced no text (e.g. it ended on a tool call or an error),
     fall back to concatenating every text part in the stream, so a report that
     landed in an earlier step is never silently lost.

`extract_error` surfaces the last stream `error` event for drop diagnostics.

Usage: parse_events.py <events.ndjson>   # prints final text to stdout
"""
import json
import sys


def _events(path: str):
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(evt, dict):
            yield evt


def _text_of(evt: dict) -> str:
    if evt.get("type") != "text":
        return ""
    part = evt.get("part") or {}
    if part.get("type") != "text":
        return ""
    text = part.get("text")
    return text if isinstance(text, str) else ""


def extract_final_text(path: str) -> str:
    events = list(_events(path))

    last_step = -1
    for i, evt in enumerate(events):
        if evt.get("type") == "step_start":
            last_step = i

    if last_step >= 0:
        final_turn = "".join(_text_of(e) for e in events[last_step:]).strip()
        if final_turn:
            return final_turn

    return "".join(_text_of(e) for e in events).strip()


def extract_error(path: str) -> str:
    """Return a compact description of the last error event, or '' if none."""
    last = ""
    for evt in _events(path):
        if evt.get("type") != "error":
            continue
        err = evt.get("error") or evt.get("part") or {}
        if isinstance(err, dict):
            code = err.get("code") or err.get("name") or "error"
            msg = err.get("message") or err.get("path") or ""
            last = f"{code}: {msg}".strip().rstrip(": ")
        else:
            last = str(err)
    return last


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: parse_events.py <events.ndjson> [--error]", file=sys.stderr)
        return 2
    if len(sys.argv) >= 3 and sys.argv[2] == "--error":
        sys.stdout.write(extract_error(sys.argv[1]))
    else:
        sys.stdout.write(extract_final_text(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
