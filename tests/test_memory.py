#!/usr/bin/env python3
"""Tests for the unifable semantic memory store (scripts/memory/store.py).

Covers:
- add_note / get_note round-trip
- Sharded on-disk paths (<data_root>/memory/<category>/<area>/<id>.json)
- TF-IDF search ranking (most-relevant note returned first)
- Tag filter in search
- Category filter in search and list_notes
- link() + neighbors() (knowledge graph)
- Secret redaction on write

Run:
    cd /path/to/unifable && python -m pytest tests/test_memory.py -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Make sure the repo root is on sys.path so relative imports inside store.py
# resolve correctly regardless of working directory.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_dir: str):
    """Return the store module with UNIFABLE_DATA pointed at tmp_dir."""
    import importlib
    import scripts.memory.store as store_mod
    import scripts.memory._tfidf as tfidf_mod

    # Reload so data_root() picks up the freshly set env var.
    importlib.reload(tfidf_mod)
    importlib.reload(store_mod)
    return store_mod


class MemoryStoreTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="unifable_mem_test_")
        os.environ["UNIFABLE_DATA"] = self._tmp
        # Re-import with the new env var in effect.
        self.store = _make_store(self._tmp)

    def tearDown(self) -> None:
        del os.environ["UNIFABLE_DATA"]

    # ------------------------------------------------------------------
    # add_note / get_note
    # ------------------------------------------------------------------

    def test_add_and_get_round_trip(self) -> None:
        note_id = self.store.add_note(
            category="engineering",
            area="auth",
            topic="JWT signing",
            body="Use RS256 for service-to-service; HS256 for single-server.",
            tags=["jwt", "auth"],
        )
        self.assertIsInstance(note_id, str)
        self.assertTrue(len(note_id) > 0)

        retrieved = self.store.get_note(note_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["id"], note_id)
        self.assertEqual(retrieved["category"], "engineering")
        self.assertEqual(retrieved["area"], "auth")
        self.assertEqual(retrieved["topic"], "JWT signing")
        self.assertIn("jwt", retrieved["tags"])

    def test_get_nonexistent_returns_none(self) -> None:
        result = self.store.get_note("does-not-exist-00000000")
        self.assertIsNone(result)

    # ------------------------------------------------------------------
    # Sharded paths
    # ------------------------------------------------------------------

    def test_shard_path_matches_category_area(self) -> None:
        note_id = self.store.add_note(
            category="research",
            area="embeddings",
            topic="FAISS vs Annoy",
            body="FAISS scales better for >1M vectors.",
        )
        mem_root = Path(self._tmp) / "memory"
        # The note file must live under memory/research/embeddings/
        matches = list(mem_root.glob(f"research/embeddings/{note_id}.json"))
        self.assertEqual(len(matches), 1, "Note file not found at expected sharded path")

    def test_shard_path_slugifies_spaces_and_uppercase(self) -> None:
        note_id = self.store.add_note(
            category="My Category",
            area="Sub Area",
            topic="topic",
            body="body text",
        )
        mem_root = Path(self._tmp) / "memory"
        # Slugified: "my_category" / "sub_area"
        matches = list(mem_root.glob(f"my_category/sub_area/{note_id}.json"))
        self.assertEqual(len(matches), 1, "Slugified sharded path not found")

    def test_note_file_is_valid_json(self) -> None:
        note_id = self.store.add_note(
            category="ops",
            area="ci",
            topic="caching strategy",
            body="Cache ~/.cargo and target/ separately.",
        )
        mem_root = Path(self._tmp) / "memory"
        paths = list(mem_root.rglob(f"{note_id}.json"))
        self.assertEqual(len(paths), 1)
        data = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(data["id"], note_id)

    # ------------------------------------------------------------------
    # TF-IDF search ranking
    # ------------------------------------------------------------------

    def test_tfidf_search_returns_most_relevant_first(self) -> None:
        # Note A is about Rust memory safety (high relevance to "Rust ownership borrow")
        id_a = self.store.add_note(
            category="engineering",
            area="rust",
            topic="ownership and borrowing",
            body="Rust ownership rules prevent data races at compile time. Borrow checker enforces single mutable reference.",
            tags=["rust", "memory", "ownership"],
        )
        # Note B is about Python async (low relevance to the query)
        id_b = self.store.add_note(
            category="engineering",
            area="python",
            topic="asyncio event loop",
            body="Use asyncio.run() in Python 3.7+. Avoid blocking calls inside coroutines.",
            tags=["python", "async"],
        )
        # Note C is a brief mention of databases (irrelevant)
        _id_c = self.store.add_note(
            category="engineering",
            area="database",
            topic="connection pooling",
            body="Use pgbouncer for PostgreSQL connection pooling in production.",
            tags=["postgres", "database"],
        )

        results = self.store.search("Rust ownership borrow checker memory safety", k=3)
        self.assertTrue(len(results) >= 1)
        # The Rust note must be ranked first
        self.assertEqual(results[0]["id"], id_a, "Rust note should be ranked first")
        # Python async note should rank lower than Rust note
        ids_in_order = [r["id"] for r in results]
        if id_b in ids_in_order:
            self.assertGreater(
                ids_in_order.index(id_b),
                ids_in_order.index(id_a),
                "Python async note should rank below the Rust note",
            )

    def test_search_returns_empty_when_no_notes(self) -> None:
        results = self.store.search("anything at all", k=5)
        self.assertEqual(results, [])

    def test_search_respects_k_limit(self) -> None:
        for i in range(6):
            self.store.add_note(
                category="misc",
                area="test",
                topic=f"note {i}",
                body=f"content about topic number {i} with some filler words here",
            )
        results = self.store.search("content topic", k=3)
        self.assertLessEqual(len(results), 3)

    # ------------------------------------------------------------------
    # Tag filter
    # ------------------------------------------------------------------

    def test_search_tag_filter_excludes_untagged(self) -> None:
        id_tagged = self.store.add_note(
            category="engineering",
            area="security",
            topic="TLS handshake",
            body="TLS 1.3 reduces round trips and drops older cipher suites.",
            tags=["tls", "security"],
        )
        _id_untagged = self.store.add_note(
            category="engineering",
            area="security",
            topic="firewall rules",
            body="TLS and firewall rules interact when deep packet inspection is enabled.",
        )
        results = self.store.search("TLS security", k=5, tag="tls")
        result_ids = [r["id"] for r in results]
        self.assertIn(id_tagged, result_ids)
        self.assertNotIn(_id_untagged, result_ids)

    # ------------------------------------------------------------------
    # Category filter
    # ------------------------------------------------------------------

    def test_search_category_filter(self) -> None:
        id_eng = self.store.add_note(
            category="engineering",
            area="api",
            topic="rate limiting",
            body="Apply token bucket rate limiting per API key.",
            tags=["api"],
        )
        id_ops = self.store.add_note(
            category="ops",
            area="api",
            topic="rate limiting",
            body="Nginx rate limit module with token bucket for API endpoints.",
            tags=["api"],
        )
        results = self.store.search("rate limiting api token bucket", k=5, category="engineering")
        result_ids = [r["id"] for r in results]
        self.assertIn(id_eng, result_ids)
        self.assertNotIn(id_ops, result_ids)

    def test_list_notes_category_filter(self) -> None:
        self.store.add_note("engineering", "frontend", "React hooks", "useState and useEffect basics.")
        self.store.add_note("ops", "deploy", "Blue-green", "Use blue-green deployments to avoid downtime.")
        eng_notes = self.store.list_notes(category="engineering")
        ops_notes = self.store.list_notes(category="ops")
        self.assertEqual(len(eng_notes), 1)
        self.assertEqual(eng_notes[0]["category"], "engineering")
        self.assertEqual(len(ops_notes), 1)
        self.assertEqual(ops_notes[0]["category"], "ops")

    def test_list_notes_all(self) -> None:
        self.store.add_note("engineering", "a", "topic 1", "body 1")
        self.store.add_note("research", "b", "topic 2", "body 2")
        all_notes = self.store.list_notes()
        self.assertEqual(len(all_notes), 2)

    # ------------------------------------------------------------------
    # link / neighbors (knowledge graph)
    # ------------------------------------------------------------------

    def test_link_and_neighbors_bidirectional(self) -> None:
        id_a = self.store.add_note("engineering", "auth", "OAuth2 flow", "Authorization code flow with PKCE.")
        id_b = self.store.add_note("engineering", "auth", "PKCE", "Proof Key for Code Exchange prevents auth code interception.")

        self.store.link(id_a, id_b)

        nb_a = self.store.neighbors(id_a)
        nb_b = self.store.neighbors(id_b)
        self.assertIn(id_b, nb_a, "id_b should appear in neighbors of id_a")
        self.assertIn(id_a, nb_b, "id_a should appear in neighbors of id_b")

    def test_neighbors_empty_for_unlinked_note(self) -> None:
        note_id = self.store.add_note("research", "ml", "attention", "Multi-head attention explained.")
        self.assertEqual(self.store.neighbors(note_id), [])

    def test_links_field_updated_in_note_file(self) -> None:
        id_a = self.store.add_note("engineering", "db", "indexing", "B-tree indexes for range queries.")
        id_b = self.store.add_note("engineering", "db", "query planner", "Postgres query planner uses statistics.")
        self.store.link(id_a, id_b)
        note_a = self.store.get_note(id_a)
        self.assertIn(id_b, note_a["links"])

    def test_add_note_with_initial_links(self) -> None:
        id_a = self.store.add_note("engineering", "infra", "terraform", "Terraform state locking with DynamoDB.")
        id_b = self.store.add_note(
            "engineering", "infra", "s3 backend",
            "Use S3 + DynamoDB for remote Terraform state.",
            links=[id_a],
        )
        nb_a = self.store.neighbors(id_a)
        nb_b = self.store.neighbors(id_b)
        self.assertIn(id_b, nb_a)
        self.assertIn(id_a, nb_b)

    # ------------------------------------------------------------------
    # Secret redaction
    # ------------------------------------------------------------------

    def test_secrets_redacted_on_write(self) -> None:
        note_id = self.store.add_note(
            category="ops",
            area="secrets",
            topic="leaked key example",
            body="api_key=sk-abc123XYZsupersecretvalue deployed to prod",
            tags=["secret"],
        )
        note = self.store.get_note(note_id)
        self.assertNotIn("sk-abc123XYZsupersecretvalue", note["body"])
        self.assertIn("[REDACTED]", note["body"])

    # ------------------------------------------------------------------
    # Graph file
    # ------------------------------------------------------------------

    def test_graph_file_created_after_link(self) -> None:
        id_a = self.store.add_note("research", "nlp", "tokenization", "WordPiece vs BPE tokenizers.")
        id_b = self.store.add_note("research", "nlp", "vocabulary", "Vocabulary size affects perplexity.")
        self.store.link(id_a, id_b)
        graph_path = Path(self._tmp) / "memory" / "_graph.json"
        self.assertTrue(graph_path.exists(), "_graph.json should exist after link()")
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        self.assertIn(id_a, graph)
        self.assertIn(id_b, graph)


if __name__ == "__main__":
    unittest.main(verbosity=2)
