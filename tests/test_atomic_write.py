#!/usr/bin/env python3
"""Regression tests for the gate's atomic, concurrency-safe writes.

Guards the [Errno 2] No such file or directory: '<id>.tmp' -> '<id>.json' crash
that the fixed-".tmp" name caused when concurrent gate hooks (parallel tool calls
in one turn) wrote the same per-session ledger. write_text_atomic gives every
writer a unique temp, so the rename never races. Run:
    python3 tests/test_atomic_write.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "gate"))

import findings as F  # noqa: E402
import ledger as L  # noqa: E402
from atomicio import _sweep_orphan_temps, write_text_atomic  # noqa: E402


class TestAtomicWrite(unittest.TestCase):
    def test_concurrent_same_path_no_enoent(self) -> None:
        """Many writers hammering one path must never raise (the reported bug)."""
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "ledgers" / "session.json"  # nested: dir does not exist yet
            errors: list[str] = []

            def writer(i: int) -> None:
                try:
                    for _ in range(20):
                        write_text_atomic(target, json.dumps({"writer": i}))
                except BaseException as exc:  # noqa: BLE001
                    errors.append(repr(exc))

            with ThreadPoolExecutor(max_workers=24) as pool:
                list(pool.map(writer, range(24)))

            self.assertEqual(errors, [], f"writers raised: {errors[:3]}")
            self.assertTrue(target.exists())
            # final content is valid JSON from exactly one writer (last-writer-wins)
            payload = json.loads(target.read_text())
            self.assertIn(payload["writer"], range(24))

    def test_no_temp_files_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "a.json"

            def writer(i: int) -> None:
                for _ in range(30):
                    write_text_atomic(target, json.dumps({"i": i}))

            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(writer, range(16)))

            leftovers = [p.name for p in target.parent.iterdir() if p.name != "a.json"]
            self.assertEqual(leftovers, [], f"temp files leaked: {leftovers}")

    def test_creates_missing_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "x" / "y" / "z.json"
            write_text_atomic(target, "hello")
            self.assertEqual(target.read_text(), "hello")

    def test_ledger_save_concurrent(self) -> None:
        """save_ledger (the actual reported call site) is race-free under load."""
        with tempfile.TemporaryDirectory() as d:
            import os

            os.environ["UNIFABLE_DATA"] = d
            input_data = {"session_id": "abc123def456"}
            errors: list[str] = []

            def saver(i: int) -> None:
                try:
                    for _ in range(15):
                        led = L.load_ledger(input_data)
                        L.add_unique(led, "verification_commands", f"cmd-{i}")
                        L.save_ledger(input_data, led)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(repr(exc))

            threads = [threading.Thread(target=saver, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [], f"save_ledger raised: {errors[:3]}")
            # final ledger is readable + valid
            final = L.load_ledger(input_data)
            self.assertIsInstance(final, dict)
            self.assertIn("verification_commands", final)


class TestOrphanTempSweep(unittest.TestCase):
    def test_old_orphan_temp_is_reaped(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "ledger.json"
            # simulate a temp orphaned by a hard kill, aged past the threshold
            orphan = Path(d) / "ledger.json.deadbeef.tmp"
            orphan.write_text("partial")
            old = time.time() - 600
            os.utime(orphan, (old, old))
            # a fresh write triggers the targeted sweep
            write_text_atomic(target, "ok")
            self.assertFalse(orphan.exists(), "stale orphan temp should be reaped")
            self.assertEqual(target.read_text(), "ok")

    def test_recent_temp_is_not_reaped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "ledger.json"
            recent = Path(d) / "ledger.json.feedface.tmp"
            recent.write_text("in-flight")  # mtime = now, another writer's live temp
            write_text_atomic(target, "ok")
            self.assertTrue(recent.exists(), "a recent (in-flight) temp must be left alone")

    def test_sweep_never_raises_on_missing_dir(self) -> None:
        _sweep_orphan_temps(Path("/nonexistent/dir/xyz"), "a.json")  # must not raise


class TestFindingsConcurrency(unittest.TestCase):
    def test_concurrent_add_finding_loses_nothing(self) -> None:
        """The Stop-blocking findings store must not drop findings under concurrent
        adds (the counter-collision + last-writer-wins lost-finding bug)."""
        import os

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as data:
            os.environ["UNIFABLE_DATA"] = data
            errors: list[str] = []

            def adder(i: int) -> None:
                try:
                    F.add_finding(root, f"bug number {i}", "high", evidence=f"e{i}")
                except BaseException as exc:  # noqa: BLE001
                    errors.append(repr(exc))

            with ThreadPoolExecutor(max_workers=24) as pool:
                list(pool.map(adder, range(24)))

            self.assertEqual(errors, [], f"add_finding raised: {errors[:3]}")
            data = F.load_findings(root)
            # all 24 must survive — no clobbered ids, no lost snapshot
            self.assertEqual(len(data["findings"]), 24, "concurrent adds lost a finding")
            self.assertEqual(len(F.blocking_findings(root)), 24)


if __name__ == "__main__":
    unittest.main(verbosity=2)
