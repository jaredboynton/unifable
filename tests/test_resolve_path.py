#!/usr/bin/env python3
"""Regression: resolve_path must not segfault under heavy concurrent use."""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "gate"))

import ledger as L  # noqa: E402


def test_resolve_path_concurrent_no_crash() -> None:
    with tempfile.TemporaryDirectory() as d:
        expected = L.resolve_path(d)
        errors: list[str] = []
        seen: set[Path] = set()
        lock = threading.Lock()

        def hammer() -> None:
            try:
                for _ in range(4000):
                    resolved = L.resolve_path(d)
                    with lock:
                        seen.add(resolved)
            except BaseException as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=hammer) for _ in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"resolve_path raised: {errors[:3]}"
        assert seen == {expected}
