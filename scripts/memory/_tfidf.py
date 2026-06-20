#!/usr/bin/env python3
"""Pure-Python TF-IDF + cosine similarity for the unifable memory layer.

Swap point: replace _embed(text) with a real embedding call (e.g.
sentence-transformers, OpenAI embeddings, Ollama) and replace
_cosine(a, b) with numpy dot if you want dense vectors.  The public
interface (build_index / search_index) is unchanged.
"""

from __future__ import annotations

import math
import re
from typing import Any


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric, drop single-char tokens."""
    return [tok for tok in re.split(r"[^a-z0-9]+", text.lower()) if len(tok) > 1]


def _tf(tokens: list[str]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for tok in tokens:
        counts[tok] = counts.get(tok, 0) + 1
    total = max(len(tokens), 1)
    return {tok: count / total for tok, count in counts.items()}


def _idf(corpus_tokens: list[list[str]]) -> dict[str, float]:
    N = len(corpus_tokens)
    if N == 0:
        return {}
    df: dict[str, int] = {}
    for tokens in corpus_tokens:
        for tok in set(tokens):
            df[tok] = df.get(tok, 0) + 1
    return {tok: math.log((N + 1) / (count + 1)) + 1.0 for tok, count in df.items()}


def _tfidf_vec(tf_map: dict[str, float], idf_map: dict[str, float]) -> dict[str, float]:
    return {tok: tf_val * idf_map.get(tok, 1.0) for tok, tf_val in tf_map.items()}


def _norm(vec: dict[str, float]) -> float:
    return math.sqrt(sum(v * v for v in vec.values()))


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    na, nb = _norm(a), _norm(b)
    if na == 0 or nb == 0:
        return 0.0
    dot = sum(a.get(tok, 0.0) * val for tok, val in b.items())
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Public index API
# ---------------------------------------------------------------------------

def build_index(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """Build an in-memory TF-IDF index over a list of note dicts.

    Each note must have at least an 'id' key. All string values are
    concatenated for vectorization (body, topic, area, category, tags).

    Returns an opaque index dict passed back to search_index().

    Swap point: replace this function body with a call to an embedding
    model to store dense vectors instead of sparse TF-IDF maps.
    """
    corpus_tokens: list[list[str]] = []
    doc_ids: list[str] = []

    for note in notes:
        text = _note_text(note)
        tokens = _tokenize(text)
        corpus_tokens.append(tokens)
        doc_ids.append(note["id"])

    idf_map = _idf(corpus_tokens)
    doc_vecs: list[dict[str, float]] = []
    for tokens in corpus_tokens:
        tf_map = _tf(tokens)
        doc_vecs.append(_tfidf_vec(tf_map, idf_map))

    return {"idf": idf_map, "doc_ids": doc_ids, "doc_vecs": doc_vecs, "notes": notes}


def search_index(
    index: dict[str, Any],
    query: str,
    k: int = 5,
    category: str | None = None,
    tag: str | None = None,
) -> list[dict[str, Any]]:
    """Return up to k notes ranked by cosine similarity to query.

    Swap point: replace cosine call with a vector DB lookup if doc_vecs
    are replaced with dense embedding vectors.
    """
    if not index["doc_ids"]:
        return []

    q_tokens = _tokenize(query)
    q_tf = _tf(q_tokens)
    q_vec = _tfidf_vec(q_tf, index["idf"])

    scored: list[tuple[float, dict[str, Any]]] = []
    for note, vec in zip(index["notes"], index["doc_vecs"]):
        if category is not None and note.get("category") != category:
            continue
        if tag is not None and tag not in note.get("tags", []):
            continue
        score = _cosine(q_vec, vec)
        scored.append((score, note))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [note for _, note in scored[:k]]


def _note_text(note: dict[str, Any]) -> str:
    parts = [
        note.get("category", ""),
        note.get("area", ""),
        note.get("topic", ""),
        note.get("body", ""),
        " ".join(note.get("tags", [])),
    ]
    return " ".join(p for p in parts if p)
