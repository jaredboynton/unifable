#!/usr/bin/env python3
"""Tests for scripts/gate/findings.py.

Covers: add, list, open_findings, blocking_findings, resolve, reject lifecycle.
All state is isolated to a tempdir per test.
"""

import sys
import tempfile
import unittest
from pathlib import Path

# Allow direct run from the repo root or from tests/
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent / "scripts" / "gate"),
)
import findings as F


class FindingsBase(unittest.TestCase):
    def setUp(self):
        import os

        self._tmp = tempfile.mkdtemp(prefix="unifable_findings_test_")
        self.root = self._tmp
        # Isolate the consolidated DB to a per-test data root so findings rows
        # never touch the real ~/.unifable or bleed across tests.
        self._data = tempfile.mkdtemp(prefix="unifable_findings_data_")
        self._prev_data = os.environ.get("UNIFABLE_DATA")
        os.environ["UNIFABLE_DATA"] = self._data

    def tearDown(self):
        import os
        import shutil

        if self._prev_data is None:
            os.environ.pop("UNIFABLE_DATA", None)
        else:
            os.environ["UNIFABLE_DATA"] = self._prev_data
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self._data, ignore_errors=True)


class TestLoadSaveEmpty(FindingsBase):
    def test_load_empty(self):
        data = F.load_findings(self.root)
        self.assertEqual(data["findings"], {})
        self.assertEqual(data["counter"], 0)

    def test_save_persists_to_db(self):
        # Findings now live in the consolidated SQLite DB, not a JSON file.
        F.add_finding(self.root, "Persisted via db", "high")
        reloaded = F.load_findings(self.root)
        self.assertEqual(len(reloaded["findings"]), 1)
        db_file = Path(self._data) / "unifable.db"
        self.assertTrue(db_file.exists())


class TestAdd(FindingsBase):
    def test_add_returns_id(self):
        fid = F.add_finding(self.root, "SQL injection in login", "high")
        self.assertIn("sql-injection", fid)

    def test_add_increments_counter(self):
        fid1 = F.add_finding(self.root, "Issue alpha", "low")
        fid2 = F.add_finding(self.root, "Issue beta", "medium")
        self.assertTrue(fid1.endswith("-1"))
        self.assertTrue(fid2.endswith("-2"))

    def test_add_default_status_open(self):
        fid = F.add_finding(self.root, "Race condition", "critical")
        f = F.get_finding(self.root, fid)
        self.assertEqual(f["status"], "open")

    def test_add_stores_all_fields(self):
        fid = F.add_finding(
            self.root,
            "Memory leak",
            "medium",
            source="code-review",
            location="src/server.py:42",
            evidence="valgrind output",
        )
        f = F.get_finding(self.root, fid)
        self.assertEqual(f["severity"], "medium")
        self.assertEqual(f["source"], "code-review")
        self.assertEqual(f["location"], "src/server.py:42")
        self.assertEqual(f["evidence"], "valgrind output")

    def test_add_invalid_severity_raises(self):
        with self.assertRaises(ValueError):
            F.add_finding(self.root, "Bad finding", "extreme")

    def test_add_persists_across_load(self):
        fid = F.add_finding(self.root, "Persisted finding", "low")
        # Force a fresh load from disk
        reloaded = F.load_findings(self.root)
        self.assertIn(fid, reloaded["findings"])


class TestOpenFindings(FindingsBase):
    def test_open_findings_returns_open_only(self):
        fid1 = F.add_finding(self.root, "Open one", "low")
        fid2 = F.add_finding(self.root, "Open two", "high")
        fid3 = F.add_finding(self.root, "Will resolve", "medium")
        F.set_status(self.root, fid3, "resolved", resolution="fixed in v2")
        open_ids = {f["id"] for f in F.open_findings(self.root)}
        self.assertIn(fid1, open_ids)
        self.assertIn(fid2, open_ids)
        self.assertNotIn(fid3, open_ids)

    def test_open_findings_empty_store(self):
        self.assertEqual(F.open_findings(self.root), [])


class TestBlockingFindings(FindingsBase):
    def test_blocking_high_and_critical_open(self):
        fid_h = F.add_finding(self.root, "High open", "high")
        fid_c = F.add_finding(self.root, "Critical open", "critical")
        fid_l = F.add_finding(self.root, "Low open", "low")
        fid_m = F.add_finding(self.root, "Medium open", "medium")
        blocking_ids = {f["id"] for f in F.blocking_findings(self.root)}
        self.assertIn(fid_h, blocking_ids)
        self.assertIn(fid_c, blocking_ids)
        self.assertNotIn(fid_l, blocking_ids)
        self.assertNotIn(fid_m, blocking_ids)

    def test_blocking_includes_blocked_status(self):
        fid = F.add_finding(self.root, "High blocked", "high")
        F.set_status(self.root, fid, "blocked")
        blocking_ids = {f["id"] for f in F.blocking_findings(self.root)}
        self.assertIn(fid, blocking_ids)

    def test_resolved_high_not_blocking(self):
        fid = F.add_finding(self.root, "High resolved", "high")
        F.set_status(self.root, fid, "resolved", resolution="patched")
        self.assertEqual(F.blocking_findings(self.root), [])

    def test_rejected_critical_not_blocking(self):
        fid = F.add_finding(self.root, "Critical rejected", "critical")
        F.set_status(self.root, fid, "rejected", resolution="false positive")
        self.assertEqual(F.blocking_findings(self.root), [])

    def test_no_findings_not_blocking(self):
        self.assertEqual(F.blocking_findings(self.root), [])


class TestResolve(FindingsBase):
    def test_resolve_sets_status_and_resolution(self):
        fid = F.add_finding(self.root, "Bug to fix", "high")
        F.set_status(
            self.root,
            fid,
            "resolved",
            resolution="deployed patch",
            verify_cmd="pytest tests/",
            verify_evidence="5 passed",
        )
        f = F.get_finding(self.root, fid)
        self.assertEqual(f["status"], "resolved")
        self.assertEqual(f["resolution"], "deployed patch")
        self.assertEqual(f["verify_cmd"], "pytest tests/")
        self.assertEqual(f["verify_evidence"], "5 passed")

    def test_resolve_removes_from_blocking(self):
        fid = F.add_finding(self.root, "Critical bug", "critical")
        self.assertEqual(len(F.blocking_findings(self.root)), 1)
        F.set_status(self.root, fid, "resolved", resolution="fixed")
        self.assertEqual(F.blocking_findings(self.root), [])

    def test_invalid_status_raises(self):
        fid = F.add_finding(self.root, "Some bug", "low")
        with self.assertRaises(ValueError):
            F.set_status(self.root, fid, "wontfix")

    def test_unknown_id_raises(self):
        with self.assertRaises(KeyError):
            F.set_status(self.root, "nonexistent-1", "resolved", resolution="nope")


class TestReject(FindingsBase):
    def test_reject_sets_status(self):
        fid = F.add_finding(self.root, "Spurious finding", "medium")
        F.set_status(self.root, fid, "rejected", resolution="not reproducible")
        f = F.get_finding(self.root, fid)
        self.assertEqual(f["status"], "rejected")
        self.assertEqual(f["resolution"], "not reproducible")

    def test_rejected_not_in_open_findings(self):
        fid = F.add_finding(self.root, "False alarm", "high")
        F.set_status(self.root, fid, "rejected", resolution="false positive")
        open_ids = {f["id"] for f in F.open_findings(self.root)}
        self.assertNotIn(fid, open_ids)


class TestGetFinding(FindingsBase):
    def test_get_existing(self):
        fid = F.add_finding(self.root, "Existing", "low")
        f = F.get_finding(self.root, fid)
        self.assertIsNotNone(f)
        self.assertEqual(f["id"], fid)

    def test_get_missing_returns_none(self):
        self.assertIsNone(F.get_finding(self.root, "no-such-1"))


class TestAtomicSave(FindingsBase):
    def test_no_tmp_file_left_after_save(self):
        # SQLite WAL handles durability; no sidecar temp file is created in the
        # project tree (the legacy atomicio .tmp path must never appear).
        F.add_finding(self.root, "Atomic test", "low")
        tmp = Path(self.root) / ".unifable" / "findings.tmp"
        self.assertFalse(tmp.exists())


class TestFullLifecycle(FindingsBase):
    def test_add_block_resolve_unblock(self):
        # Add a blocker
        fid = F.add_finding(self.root, "Auth bypass", "critical", source="pen-test")
        self.assertEqual(len(F.blocking_findings(self.root)), 1)
        # Low severity finding does not add to blockers
        F.add_finding(self.root, "Typo in docs", "low")
        self.assertEqual(len(F.blocking_findings(self.root)), 1)
        # Resolve the blocker
        F.set_status(
            self.root,
            fid,
            "resolved",
            resolution="patched in commit abc123",
            verify_cmd="./run_auth_tests.sh",
            verify_evidence="all 12 auth tests passed",
        )
        # No more blockers
        self.assertEqual(F.blocking_findings(self.root), [])
        # Still appears in full list but not open_findings
        f = F.get_finding(self.root, fid)
        self.assertEqual(f["status"], "resolved")
        open_ids = {f["id"] for f in F.open_findings(self.root)}
        self.assertNotIn(fid, open_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
