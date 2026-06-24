"""Pytest collection config.

test_gate_robustness.py is a standalone script (module-level checks + sys.exit),
run directly by scripts/commit.sh (`python3 tests/test_gate_robustness.py`). It is
not importable as a pytest module, so exclude it from collection here -- this is
what commit.sh's `--ignore` did, baked in so a bare `pytest tests/` also works.

The warm-socket judge daemon (judge_daemon.py) is an opt-in latency/cost
optimization. It is disabled by default during tests so no test can accidentally
spawn a background daemon or make a live Realtime call through a hook subprocess;
the direct codex_judge path is exercised exactly as before. Daemon-specific tests
override UNIFABLE_JUDGE_DAEMON via monkeypatch.setenv.
"""

import os

os.environ.setdefault("UNIFABLE_JUDGE_DAEMON", "0")

collect_ignore = ["test_gate_robustness.py"]
