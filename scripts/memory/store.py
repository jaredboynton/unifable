#!/usr/bin/env python3
"""unifable local semantic memory store.

Notes are compact records with a Category / Area / Topic taxonomy plus a short
body, tags, and links (ids of related notes, forming a lightweight knowledge
graph).  Stored sharded on disk under:

    <data_root>/memory/<category>/<area>/<id>.json

data_root() follows the same convention as ledger.py: ~/.unifable/ by default,
overridden by the UNIFABLE_DATA env var.

Public Python API
-----------------
    add_note(category, area, topic, body, tags=(), links=()) -> str (id)
    get_note(id) -> dict | None
    search(query, k=5, category=None, tag=None) -> list[dict]
    link(id_a, id_b) -> None
    list_notes(category=None) -> list[dict]
    neighbors(id) -> list[str]

CLI
---
    python store.py add   <category> <area> <topic> <body> [--tags t1,t2] [--links id1,id2]
    python store.py search <query> [--k N] [--category C] [--tag T]
    python store.py show  <id>
    python store.py link  <id_a> <id_b>
    python store.py list  [--category C]

On-disk layout
--------------
    <data_root>/memory/<category>/<area>/<id>.json   -- one file per note
    <data_root>/memory/_graph.json                   -- {id: [linked_ids]}

Secret redaction is applied to all string fields on write (same patterns as
ledger.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Secret redaction (mirrors ledger.py SECRET_PATTERNS)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{12,}"),
]


def _redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def _redact_note(note: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in note.items():
        if isinstance(val, str):
            out[key] = _redact(val)
        elif isinstance(val, list):
            out[key] = [_redact(v) if isinstance(v, str) else v for v in val]
        else:
            out[key] = val
    return out


# ---------------------------------------------------------------------------
# Path helpers (same convention as ledger.data_root)
# ---------------------------------------------------------------------------

def data_root() -> Path:
    env = os.environ.get("UNIFABLE_DATA")
    base = Path(env).expanduser() if env else Path.home() / ".unifable"
    return base.resolve()


def _memory_root() -> Path:
    return data_root() / "memory"


def _graph_path() -> Path:
    return _memory_root() / "_graph.json"


def _note_path(category: str, area: str, note_id: str) -> Path:
    return _memory_root() / _slug(category) / _slug(area) / f"{note_id}.json"


def _slug(s: str) -> str:
    """Convert a string to a safe filesystem component."""
    return re.sub(r"[^a-z0-9_-]", "_", s.lower().strip())


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _make_id(category: str, area: str, topic: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    raw = f"{category}/{area}/{topic}/{ts}"
    short = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{_slug(category)}-{short}"


# ---------------------------------------------------------------------------
# Atomic save helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

def _load_graph() -> dict[str, list[str]]:
    return _load_json(_graph_path(), {})


def _save_graph(graph: dict[str, list[str]]) -> None:
    _atomic_write(_graph_path(), graph)


# ---------------------------------------------------------------------------
# Note discovery (iterates shards)
# ---------------------------------------------------------------------------

def _iter_notes(category: str | None = None) -> list[dict[str, Any]]:
    root = _memory_root()
    if not root.exists():
        return []
    notes: list[dict[str, Any]] = []
    for path in root.rglob("*.json"):
        if path.name.startswith("_"):
            continue  # skip _graph.json and any future index files
        note = _load_json(path, None)
        if not isinstance(note, dict) or "id" not in note:
            continue
        if category is not None and note.get("category") != category:
            continue
        notes.append(note)
    return notes


# ---------------------------------------------------------------------------
# Public Python API
# ---------------------------------------------------------------------------

def add_note(
    category: str,
    area: str,
    topic: str,
    body: str,
    tags: list[str] | tuple[str, ...] = (),
    links: list[str] | tuple[str, ...] = (),
) -> str:
    """Create and persist a new note. Returns the generated id."""
    note_id = _make_id(category, area, topic)
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    note: dict[str, Any] = {
        "id": note_id,
        "category": category,
        "area": area,
        "topic": topic,
        "body": body,
        "tags": list(tags),
        "links": list(links),
        "created_at": ts,
    }
    note = _redact_note(note)
    path = _note_path(category, area, note_id)
    _atomic_write(path, note)

    # Register any initial links in the graph
    if links:
        graph = _load_graph()
        existing = list(graph.get(note_id, []))
        for other_id in links:
            if other_id not in existing:
                existing.append(other_id)
            # bidirectional
            other_links = list(graph.get(other_id, []))
            if note_id not in other_links:
                other_links.append(note_id)
                graph[other_id] = other_links
        graph[note_id] = existing
        _save_graph(graph)

    return note_id


def get_note(note_id: str) -> dict[str, Any] | None:
    """Return a note by id, or None if not found."""
    root = _memory_root()
    if not root.exists():
        return None
    for path in root.rglob(f"{note_id}.json"):
        note = _load_json(path, None)
        if isinstance(note, dict) and note.get("id") == note_id:
            return note
    return None


def search(
    query: str,
    k: int = 5,
    category: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    """Return up to k notes ranked by TF-IDF cosine similarity to query.

    Swap point: replace _tfidf.build_index / search_index with a dense
    embedding model (sentence-transformers, OpenAI, Ollama) without
    changing the callers.
    """
    # Import here to keep the module importable even if _tfidf is not on
    # the path during unit tests that stub the function. Resolve the sibling
    # module across all invocation contexts: as a package (tests put repo-root
    # on sys.path) and as a direct script (`python3 scripts/memory/store.py`,
    # where no `scripts` package exists on the path).
    try:
        from scripts.memory._tfidf import build_index, search_index  # type: ignore[import]
    except ModuleNotFoundError:
        try:
            from ._tfidf import build_index, search_index  # type: ignore[import]
        except ImportError:
            import os
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from _tfidf import build_index, search_index  # type: ignore[import]

    notes = _iter_notes(category=category)
    if not notes:
        return []
    index = build_index(notes)
    return search_index(index, query, k=k, category=category, tag=tag)


def link(id_a: str, id_b: str) -> None:
    """Add a bidirectional link between two notes in the knowledge graph."""
    graph = _load_graph()

    def _add_edge(src: str, dst: str) -> None:
        edges = list(graph.get(src, []))
        if dst not in edges:
            edges.append(dst)
        graph[src] = edges

    _add_edge(id_a, id_b)
    _add_edge(id_b, id_a)
    _save_graph(graph)

    # Also update the links field inside each note file for portability
    for target_id, other_id in ((id_a, id_b), (id_b, id_a)):
        note = get_note(target_id)
        if note is None:
            continue
        note_links: list[str] = list(note.get("links", []))
        if other_id not in note_links:
            note_links.append(other_id)
            note["links"] = note_links
            path = _note_path(note["category"], note["area"], target_id)
            _atomic_write(path, note)


def list_notes(category: str | None = None) -> list[dict[str, Any]]:
    """Return all notes, optionally filtered by category."""
    return _iter_notes(category=category)


def neighbors(note_id: str) -> list[str]:
    """Return the ids of all notes directly linked to note_id."""
    graph = _load_graph()
    return list(graph.get(note_id, []))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_add(args: argparse.Namespace) -> None:
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else []
    links_ = [l.strip() for l in args.links.split(",")] if args.links else []
    note_id = add_note(args.category, args.area, args.topic, args.body, tags=tags, links=links_)
    print(note_id)


def _cmd_search(args: argparse.Namespace) -> None:
    results = search(args.query, k=args.k, category=args.category or None, tag=args.tag or None)
    if not results:
        print("no results")
        return
    for note in results:
        print(f"{note['id']}  [{note['category']}/{note['area']}]  {note['topic']}")
        print(f"  {note['body'][:120]}")
        if note.get("tags"):
            print(f"  tags: {', '.join(note['tags'])}")
        print()


def _cmd_show(args: argparse.Namespace) -> None:
    note = get_note(args.id)
    if note is None:
        print(f"note not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(note, indent=2))


def _cmd_link(args: argparse.Namespace) -> None:
    link(args.id_a, args.id_b)
    print(f"linked {args.id_a} <-> {args.id_b}")


def _cmd_list(args: argparse.Namespace) -> None:
    notes = list_notes(category=args.category or None)
    if not notes:
        print("no notes")
        return
    for note in sorted(notes, key=lambda n: n.get("created_at", "")):
        print(f"{note['id']}  [{note['category']}/{note['area']}]  {note['topic']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="store.py",
        description="unifable semantic memory store",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="create a new note")
    p_add.add_argument("category")
    p_add.add_argument("area")
    p_add.add_argument("topic")
    p_add.add_argument("body")
    p_add.add_argument("--tags", default="", help="comma-separated tags")
    p_add.add_argument("--links", default="", help="comma-separated note ids to link")

    p_search = sub.add_parser("search", help="semantic search over notes")
    p_search.add_argument("query")
    p_search.add_argument("--k", type=int, default=5)
    p_search.add_argument("--category", default="")
    p_search.add_argument("--tag", default="")

    p_show = sub.add_parser("show", help="print a note as JSON")
    p_show.add_argument("id")

    p_link = sub.add_parser("link", help="link two notes bidirectionally")
    p_link.add_argument("id_a")
    p_link.add_argument("id_b")

    p_list = sub.add_parser("list", help="list all notes")
    p_list.add_argument("--category", default="")

    args = parser.parse_args(argv)
    {
        "add": _cmd_add,
        "search": _cmd_search,
        "show": _cmd_show,
        "link": _cmd_link,
        "list": _cmd_list,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
