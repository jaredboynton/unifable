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

for pid in "${pids[@]}"; do
  if ! wait "$pid"; then
    fail=1
  fi
done

exit "$fail"
