#!/usr/bin/env python3
"""UserPromptSubmit pack router — match task signals to verified packs.

stdin: JSON {"prompt": "..."}. stdout: hookSpecificOutput JSON when matched.
Always exits 0 (fail-open on internal errors).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ledger import emit_json, read_stdin_json
from plugin_root import resolve_plugin_root

_MANIFEST_NAME = "router-manifest.json"


@dataclass(frozen=True)
class PackRoute:
    tag: str
    label: str
    pack: str
    keywords: tuple[str, ...]
    summary: str


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
        pack = str(item.get("pack") or "").strip()
        if not tag or not pack:
            continue
        keywords = tuple(str(k).strip().lower() for k in (item.get("keywords") or []) if str(k).strip())
        routes.append(
            PackRoute(
                tag=tag,
                label=str(item.get("label") or tag).strip(),
                pack=pack,
                keywords=keywords,
                summary=str(item.get("summary") or "").strip(),
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
    header = f"[unifable:router] Matched task signals — read packs under {packs_root}/packs/:"
    bullets = [
        f"- {route.tag} ({route.label}): {route.pack} — {route.summary}"
        for route in matched
    ]
    return header + "\n" + "\n".join(bullets)


def route_prompt(prompt: str, *, root: Path) -> dict[str, Any] | None:
    routes = load_manifest(root)
    matched = match_routes(prompt, routes)
    if not matched:
        return None
    packs_root = str(root)
    ctx = format_context(matched, packs_root=packs_root)
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
        out = route_prompt(prompt, root=root)
        if out:
            emit_json(out)
    except Exception:
        return


if __name__ == "__main__":
    main()
    sys.exit(0)
