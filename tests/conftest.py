"""Pytest collection config.

test_gate_robustness.py is a standalone script (module-level checks + sys.exit),
run directly by scripts/commit.sh (`python3 tests/test_gate_robustness.py`). It is
not importable as a pytest module, so exclude it from collection here -- this is
what commit.sh's `--ignore` did, baked in so a bare `pytest tests/` also works.
"""

collect_ignore = ["test_gate_robustness.py"]
