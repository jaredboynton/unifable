# unifable task runner. Run `just` to list recipes.

# List available recipes.
_default:
    @just --list

# Set the plugin version across all four plugin dirs (plugin.json + marketplace.json)
# and setup/setup.sh, then verify no straggler of the old version remains.
# Refuses same-version and down-version targets (e.g. 1.9.30 -> 1.9.29).
# Usage: just version 1.9.4   (or: just version patch|minor|major)
version VERSION:
    python3 scripts/bump_version.py {{VERSION}}

# Regenerate rendered hook-output and judge-prompt reference docs.
generated-docs:
    python3 scripts/generate_docs.py

# Run the full Python test suite serially.
test:
    uv run --no-project --with-requirements requirements-dev.txt python -m pytest -n 0 tests -q

# Run the full Python test suite across available CPU cores.
test-parallel:
    uv run --no-project --with-requirements requirements-dev.txt python -m pytest -n auto --dist=worksteal tests -q

# Show the slowest tests in a serial run.
test-profile:
    uv run --no-project --with-requirements requirements-dev.txt python -m pytest -n 0 tests -q --durations=20 --durations-min=0

# Run pytest + eval_gate_proof + test_gate_robustness (commit.sh parity).
test-all:
    uv run --no-project --with-requirements requirements-dev.txt bash scripts/run_tests.sh

# Verify every wait/timeout grep match is accounted for in docs/testing-optimization.md.
wait-audit:
    python3 scripts/audit_waits.py
