# Test Suite Optimization

Date: 2026-06-24

## Scope

Goal: reduce full-suite wall-clock time, remove or justify arbitrary waits, add parallel execution where practical, and record before/after results.

## External references

- Pytest documents `--durations` / `--durations-min` for identifying slow tests: https://docs.pytest.org/en/stable/how-to/usage.html#profiling-test-execution-duration
- Pytest-xdist documents `pytest -n auto` for CPU-parallel execution and says `--dist=worksteal` is intended for suites with uneven test durations: https://pytest-xdist.readthedocs.io/en/stable/distribution.html
- Pytest-xdist known limitations require consistent collection order/count across workers and note worker stdout / `--pdb` limitations: https://pytest-xdist.readthedocs.io/en/stable/known-limitations.html
- Pytest flaky-test guidance calls out that parallel execution can reveal ordering or shared-state coupling: https://docs.pytest.org/en/stable/explanation/flaky.html

## Repo evidence

- `requirements-dev.txt:1` and `requirements-dev.txt:2` add `pytest` and `pytest-xdist` as explicit dev test dependencies.
- `pytest.ini:1` through `pytest.ini:3` make the default full-suite pytest invocation run with `-n auto --dist=worksteal`.
- `justfile:18` through `justfile:28` add reproducible serial, parallel, and duration-profile recipes through `uv run --with-requirements requirements-dev.txt`. The serial/profile recipes pass `-n 0` so they remain true serial measurements even with the repo-level xdist default.
- `.gitignore:3` ignores the local `.venv/` used during measurement.
- `tests/conftest.py:1` through `tests/conftest.py:19` keep collection config minimal, exclude only the standalone `test_gate_robustness.py`, and disable the warm-socket judge daemon by default for tests.
- Narrow wait audit: `rg -n "time\\.sleep|sleep\\(" tests -g '*.py'` found only `tests/test_judge_coalesce.py:47`. That wait is justified by `tests/test_judge_coalesce.py:31` through `tests/test_judge_coalesce.py:35`, which describe a counting judge that briefly holds the breaker lock to force parallel-batch contention.
- Broad audit: `rg -n "sleep\\(|time\\.sleep|wait|timeout" tests scripts hooks benchmark docs/testing-optimization.md -g '*.py' -g '*.md'` reports timeout plumbing in `benchmark/bench.py`, `hooks/test_after_edit.py`, `hooks/gate_stop.py`, and `scripts/gate/`, plus the same single test sleep. Those timeout entries are bounded subprocess, socket, lock, and hook budgets rather than arbitrary test waits.

## Measurements

Before optimization:

```text
command: python3 -m pytest tests -q --durations=20 --durations-min=0
result: 851 passed, 9 subtests passed in 65.97s
slowest test: tests/test_breaker_keying.py::test_breaker_blocks_until_validated at 10.59s
```

Refreshed serial profile after the final config change:

```text
command: just test-profile
expanded command: uv run --no-project --with-requirements requirements-dev.txt python -m pytest -n 0 tests -q --durations=20 --durations-min=0
result: 851 passed, 9 subtests passed in 98.34s
```

After adding `pytest-xdist`, `pytest.ini`, and the parallel recipe:

```text
command: just test-parallel
expanded command: uv run --no-project --with-requirements requirements-dev.txt python -m pytest -n auto --dist=worksteal tests -q
result: 851 passed, 9 subtests passed in 9.53s
```

Plain default pytest now also uses the parallel config:

```text
command: python3 -m pytest tests -q
result: 851 passed, 9 subtests passed in 9.56s
```

The exact xdist check also passes:

```text
command: python3 -m pytest -n auto tests -q
result: 851 passed, 9 subtests passed in 6.60s
```

Effect against the original serial baseline: 56.41s faster, 85.5 percent lower wall-clock time, about 6.9x faster. Effect against the refreshed serial profile: 88.78s faster, 90.3 percent lower wall-clock time, about 10.3x faster.

Install note: `python3 -m pip install -r requirements-dev.txt` failed because this Python is externally managed by uv. The reproducible repo path uses `uv run --with-requirements requirements-dev.txt`. For the exact stop-hook check that invokes system `python3 -m pytest -n auto`, the uv-managed interpreter was updated with `uv pip install --system --break-system-packages -r requirements-dev.txt`.

## Critic review

The critic agent reviewed official pytest and pytest-xdist documentation plus the current suite. Findings:

- The main realistic optimization was adding `pytest-xdist` and validating it with a whole-suite run.
- No unjustified arbitrary waits remain. The one remaining `time.sleep(0.05)` is part of a lock-contention regression test.
- Manual `os.environ` mutation remains in several tests, but the full xdist run passed, so it is not currently a measured blocker. Converting those cases to `monkeypatch` is future robustness cleanup, not a runtime optimization required for this goal.
- No current expensive immutable fixture hotspot was found that warrants moving setup to `tmp_path_factory` or broader fixture scope.

## Current recommendation

Use `python3 -m pytest tests -q` or `just test-parallel` for full-suite development and CI verification. Use `just test-profile` when the slowest-test list needs to be refreshed. Debug individual failures with serial pytest because pytest-xdist documents stdout and `--pdb` limitations under distributed workers.
