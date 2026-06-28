# unifable task runner. Run `just` to list recipes.

# List available recipes.
_default:
    @just --list

# Set the plugin version across all four plugin dirs (plugin.json + marketplace.json),
# then verify no straggler of the old version remains.
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
# UNIFABLE_JUDGE_OFFLINE=1 by default: the suite must not depend on live Realtime
# judge calls in hook subprocesses (slow, network-bound). Override per-invocation
# with `UNIFABLE_JUDGE_OFFLINE=0 just test-all` when exercising the live judge.
test-all:
    UNIFABLE_JUDGE_OFFLINE="${UNIFABLE_JUDGE_OFFLINE:-1}" uv run --no-project --with-requirements requirements-dev.txt bash scripts/run_tests.sh

# Verify every wait/timeout grep match is accounted for in docs/testing-optimization.md.
wait-audit:
    python3 scripts/audit_waits.py

# Validate AGENTS.md links and documented `just` recipes resolve against the tree.
agents-audit:
    python3 scripts/check_agents_md.py

# Lint and auto-fix all Python source (ruff check --fix).
lint:
    uv run --no-project --with-requirements requirements-dev.txt ruff check --fix hooks scripts unifable_runtime tests

# Format all Python source in place (ruff format).
format:
    uv run --no-project --with-requirements requirements-dev.txt ruff format hooks scripts unifable_runtime tests

# Run mypy strict type checking on hooks and scripts/gate.
typecheck:
    uv run --no-project --with-requirements requirements-dev.txt mypy hooks scripts/gate unifable_runtime

# Detect dead code with vulture (min-confidence 80).
dead-code:
    uv run --no-project --with-requirements requirements-dev.txt vulture hooks scripts unifable_runtime tests --min-confidence 80

# Detect unused dependencies with deptry.
unused-deps:
    uv run --no-project --with-requirements requirements-dev.txt deptry . --requirements-files requirements-dev.txt --ignore DEP001 --known-first-party scripts --known-first-party unifable_runtime

# Check cyclomatic complexity (radon cc, only grade A-C pass, D+ flagged).
complexity:
    uv run --no-project --with-requirements requirements-dev.txt radon cc -s -n C hooks scripts/gate

# Detect duplicate code (jscpd, min 6 lines / 65 tokens).
duplicate-code:
    npx jscpd

# Run pre-commit on all files (lint, format, typecheck, dead code, unused deps, complexity, duplicates, docs).
precommit:
    uv run --no-project --with-requirements requirements-dev.txt pre-commit run --all-files
