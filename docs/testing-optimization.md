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
- Narrow wait audit: `rg -n "time\\.sleep|sleep\\(" tests -g '*.py'` now returns no matches. The old `tests/test_judge_coalesce.py` sleep was replaced with barrier/event coordination so the concurrency regression still forces peer-thread contention without wall-clock slack.
- Broad audit: `rg -n "sleep\\(|time\\.sleep|wait|timeout" tests scripts hooks benchmark docs/testing-optimization.md -g '*.py' -g '*.md'` reports timeout plumbing in `benchmark/bench.py`, `hooks/test_after_edit.py`, `hooks/gate_stop.py`, and `scripts/gate/`, plus bounded thread/socket/lock waits in tests and judge transport. Those are budgets or synchronization points, not arbitrary test-suite sleeps.

## Measurements

All timing comparisons below target the same pytest suite: `tests`, with `851 passed, 9 subtests passed` in each successful full-suite run.

Before optimization, the suite ran serially:

```text
command: python3 -m pytest tests -q --durations=20 --durations-min=0
result: 851 passed, 9 subtests passed in 65.97s
slowest test: tests/test_breaker_keying.py::test_breaker_blocks_until_validated at 10.59s
```

Refreshed serial profile after the final config change, with `-n 0` to disable xdist explicitly:

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
result: 851 passed, 9 subtests passed in 9.96s
```

The exact xdist check also passes:

```text
command: python3 -m pytest -n auto tests -q
result: 851 passed, 9 subtests passed in 10.98s
```

Effect using the current default full-suite command (`python3 -m pytest tests -q`, 9.96s): 56.01s faster than the original serial baseline, 84.9 percent lower wall-clock time, about 6.6x faster. Against the refreshed serial profile, the default run is 88.38s faster, 89.9 percent lower wall-clock time, about 9.9x faster.

Install note: `python3 -m pip install -r requirements-dev.txt` failed because this Python is externally managed by uv. The reproducible repo path uses `uv run --with-requirements requirements-dev.txt`. For the exact stop-hook check that invokes system `python3 -m pytest -n auto`, the uv-managed interpreter was updated with `uv pip install --system --break-system-packages -r requirements-dev.txt`.

## Wait and timeout audit decisions

The broad audit was reviewed by category:

| Match category | Decision | Rationale |
|---|---|---|
| `tests/test_judge_coalesce.py` thread waits | Changed and kept | The previous `time.sleep` was removed. The test now uses a `threading.Barrier` plus bounded `Event.wait(timeout=2.0)` for deterministic thread coordination. Targeted verification: `python3 -m pytest tests/test_judge_coalesce.py -q` -> `5 passed in 0.64s`. |
| `benchmark/bench.py` wait/timeout paths | Kept | These are benchmark harness controls for external terminal sessions and subprocesses. They bound benchmark runs and classify timeout outcomes; removing them would make benchmark jobs hang-prone, not faster. |
| `hooks/test_after_edit.py` timeout | Kept | This is the hook subprocess budget for automatic test-after-edit checks. It is a safety bound, not an idle wait in the test suite. |
| `hooks/gate_stop.py`, `scripts/gate/spec.py`, `scripts/gate/codex_judge.py`, `scripts/gate/judge_daemon.py`, `scripts/gate/judge_transport.py`, `scripts/gate/judge_client.py`, and `scripts/gate/breaker_state.py` timeout/wait paths | Kept | These are host hook, network, socket, lock, and judge fail-open budgets. They protect interactive sessions from hanging and are outside pytest scheduling. |
| Test stubs that accept a `timeout` parameter or assert timeout handling | Kept | These tests validate timeout propagation or monkeypatch timeout-aware APIs. They do not add real waiting to normal suite execution. |
| Prose matches in docs or task examples | Kept | These are documentation or fixture strings, not executable waits. |

No remaining match is a realistic runtime optimization target. The suite-level runtime improvement came from parallel scheduling, and the post-change full-suite xdist checks pass.

## Critic review

The critic agent reviewed official pytest and pytest-xdist documentation plus the current suite. Findings:

- The main realistic optimization was adding `pytest-xdist` and validating it with a whole-suite run.
- No unjustified arbitrary waits remain. The former `time.sleep(0.05)` lock-contention test has been changed to deterministic thread synchronization.
- Manual `os.environ` mutation remains in several tests, but the full xdist run passed, so it is not currently a measured blocker. Converting those cases to `monkeypatch` is future robustness cleanup, not a runtime optimization required for this goal.
- No current expensive immutable fixture hotspot was found that warrants moving setup to `tmp_path_factory` or broader fixture scope.

## Current recommendation

Use `python3 -m pytest tests -q` or `just test-parallel` for full-suite development and CI verification. Use `just test-profile` when the slowest-test list needs to be refreshed. Debug individual failures with serial pytest because pytest-xdist documents stdout and `--pdb` limitations under distributed workers.
