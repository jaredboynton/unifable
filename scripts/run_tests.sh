#!/usr/bin/env bash
# Run the full unifable verification suite (pytest + standalone harnesses).
#
# By default pytest runs in parallel via pytest.ini (-n auto --dist worksteal).
# Set PYTEST_SERIAL=1 for a true serial pytest run (debugging ordering issues).
# Set TEST_TIMING=1 to wrap each job with /usr/bin/time -p.
#
# Usage: scripts/run_tests.sh
#
# Requires dev test deps (pytest-xdist). Install once:
#   pip install -r requirements-dev.txt
# or use: just test-all  (wraps uv run --with-requirements requirements-dev.txt)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Hermetic suite: force the Realtime judge deterministically unreachable so the
# standalone harnesses (eval_gate_proof.py, test_gate_robustness.py) — which run
# real hook subprocesses but assert fail-open / grade-driven behavior, not live
# verdicts — never make a ~1.3s live WebSocket call per hook. pytest gets the same
# default via tests/conftest.py; setting it here covers the non-pytest jobs too.
# Override (UNIFABLE_JUDGE_OFFLINE=0) only when intentionally exercising a live judge.
export UNIFABLE_JUDGE_OFFLINE="${UNIFABLE_JUDGE_OFFLINE:-1}"

SERIAL="${PYTEST_SERIAL:-0}"
TIMING="${TEST_TIMING:-0}"
pids=()

cleanup() {
  status=$?
  trap - INT TERM EXIT
  if [ "${#pids[@]}" -gt 0 ]; then
    kill "${pids[@]}" 2>/dev/null || true
    wait "${pids[@]}" 2>/dev/null || true
  fi
  exit "$status"
}

trap cleanup INT TERM EXIT

run_job() {
  if [ "$TIMING" = "1" ]; then
    /usr/bin/time -p "$@"
  else
    "$@"
  fi
}

pytest_args=(tests/ -q --ignore=tests/test_gate_robustness.py)
if [ "$SERIAL" = "1" ]; then
  pytest_args=(-n 0 "${pytest_args[@]}")
fi

fail=0

run_job python3 scripts/audit_waits.py
run_job python3 -m pytest "${pytest_args[@]}" &
pids+=("$!")
run_job python3 tests/eval_gate_proof.py &
pids+=("$!")
run_job python3 tests/test_gate_robustness.py &
pids+=("$!")
# Deterministic node unit tests for the unitrace search path (multiformat
# retrieval + rtinfer borrow). The wrapper skips its live block unless
# UNITRACE_SEARCH_LIVE=1, so this stays hermetic. Skipped if node is absent.
if command -v node >/dev/null 2>&1; then
  run_job bash skills/unitrace/scripts/test-search.sh &
  pids+=("$!")
else
  echo "node not found; skipping unitrace node search tests" >&2
fi

for pid in "${pids[@]}"; do
  if ! wait "$pid"; then
    fail=1
  fi
done

exit "$fail"
