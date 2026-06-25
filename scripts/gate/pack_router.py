#!/usr/bin/env python3
"""UserPromptSubmit pack router — match task signals and inject discipline inline.

stdin: JSON {"prompt": "..."}. stdout: hookSpecificOutput JSON when matched.
Always exits 0 (fail-open on internal errors).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from classify_task import operative_prompt
from ledger import emit_json, read_stdin_json, update_ledger
from plugin_root import resolve_plugin_root

_MANIFEST_NAME = "router-manifest.json"

# Cap how many discipline packs fire on one prompt. A prompt that genuinely spans
# many disciplines still gets the top few (manifest order); the rest are disclosed,
# not silently dropped. This bounds the injected block even after corpus-stripping.
_MAX_PACKS = 3


@dataclass(frozen=True)
class PackRoute:
    tag: str
    label: str
    keywords: tuple[str, ...]
    summary: str
    body: str


def _plugin_root() -> Path | None:
    root = resolve_plugin_root()
    if root is not None:
        return root
    candidate = Path(__file__).resolve().parents[2]
    if (candidate / "packs" / _MANIFEST_NAME).is_file():
        return candidate
    return None


def load_manifest(root: Path) -> list[PackRoute]:
    raw = json.loads((root / "packs" / _MANIFEST_NAME).read_text(encoding="utf-8"))
    routes: list[PackRoute] = []
    for item in raw.get("routes") or []:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip()
        body = str(item.get("body") or "").strip()
        if not tag or not body:
            continue
        keywords = tuple(str(k).strip().lower() for k in (item.get("keywords") or []) if str(k).strip())
        routes.append(
            PackRoute(
                tag=tag,
                label=str(item.get("label") or tag).strip(),
                keywords=keywords,
                summary=str(item.get("summary") or "").strip(),
                body=body,
            )
        )
    return routes


def match_routes(prompt: str, routes: list[PackRoute]) -> list[PackRoute]:
    low = (prompt or "").lower()
    if not low:
        return []
    matched: list[PackRoute] = []
    for route in routes:
        if any(kw in low for kw in route.keywords):
            matched.append(route)
    return matched


def format_context(matched: list[PackRoute], *, packs_root: str) -> str:
    blocks = [f"[{route.tag}] {route.label} — {route.summary}\n{route.body}" for route in matched]
    return "\n\n".join(blocks)


def _session_filtered_routes(matched: list[PackRoute], input_data: dict[str, Any] | None) -> list[PackRoute]:
    if input_data is None or not input_data.get("session_id"):
        return matched
    emitted: list[PackRoute] = []

    def updater(ledger: dict[str, Any]) -> None:
        nonlocal emitted
        fired = [str(tag) for tag in (ledger.get("router_fired_tags") or []) if str(tag)]
        fired_set = set(fired)
        emitted = [route for route in matched if route.tag not in fired_set]
        emitted_tags = [route.tag for route in emitted]
        ledger["router_matched_tags"] = emitted_tags
        for tag in emitted_tags:
            if tag not in fired_set:
                fired.append(tag)
                fired_set.add(tag)
        ledger["router_fired_tags"] = fired

    try:
        update_ledger(input_data, updater)
    except Exception:
        return matched
    return emitted


def route_prompt(prompt: str, *, root: Path, input_data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    routes = load_manifest(root)
    # Route on the operative instruction, not pasted corpus/tool output: a prompt
    # that pastes a hook dump (full of every pack's keywords) must not fire packs
    # keyed off the paste. operative_prompt() is the same slice the grade
    # classifier trusts.
    matched = match_routes(operative_prompt(prompt), routes)
    matched = _session_filtered_routes(matched, input_data)
    if not matched:
        return None
    suppressed = 0
    if len(matched) > _MAX_PACKS:
        suppressed = len(matched) - _MAX_PACKS
        matched = matched[:_MAX_PACKS]
    ctx = format_context(matched, packs_root=str(root))
    if suppressed:
        ctx += (
            f"\n\n{suppressed} more discipline pack(s) matched but were "
            f"suppressed (cap {_MAX_PACKS}); narrow the prompt to surface a specific one."
        )
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }
    }


def main() -> None:
    try:
        payload = read_stdin_json()
        prompt = str(payload.get("prompt") or "")
        if not prompt.strip():
            return
        root = _plugin_root()
        if root is None:
            return
        out = route_prompt(prompt, root=root, input_data=payload)
        if out:
            emit_json(out)
    except Exception:
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
