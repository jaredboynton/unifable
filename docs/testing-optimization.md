# Test Suite Optimization

Date: 2026-06-24

Hardware: Apple M4 Max, 16 logical CPU cores, Python 3.12.11, pytest 9.1.1.

## Goal

Reduce full-suite wall-clock time, remove or replace arbitrary waits, run independent work in parallel where safe, and record before/after measurements.

## External references

- Pytest usage / `--durations`: https://docs.pytest.org/en/stable/how-to/usage.html#profiling-test-execution-duration
- Pytest-xdist distribution (`-n auto`, `--dist worksteal`): https://pytest-xdist.readthedocs.io/en/stable/distribution.html
- Pytest-xdist known limitations (stdout, `--pdb`, collection consistency): https://pytest-xdist.readthedocs.io/en/stable/known-limitations.html
- Pytest flaky-test guidance (parallelism exposes ordering bugs): https://docs.pytest.org/en/stable/explanation/flaky.html
- Trail of Bits PyPI suite optimization (parallelize first, `testpaths`): https://blog.trailofbits.com/2025/05/01/making-pypis-test-suite-81-faster/
- pytest-test-categories sleep-blocking ADR (prefer `threading.Event` / `Barrier` over `time.sleep`): https://pytest-test-categories.readthedocs.io/en/stable/architecture/adr-005-sleep-isolation.html
- Python threading primitives: https://docs.python.org/3/library/threading.html

## Changes made

| Area | File(s) | Change |
|---|---|---|
| Parallel pytest | `pytest.ini`, `requirements-dev.txt` | Default `-n auto --dist=worksteal`; dev dep on `pytest-xdist` |
| Unified runner | `scripts/run_tests.sh`, `scripts/commit.sh` | Overlap pytest, `eval_gate_proof.py`, and `test_gate_robustness.py` |
| Parallel eval matrix | `tests/eval_gate_proof.py` | Per-scenario temp dirs + `ThreadPoolExecutor` |
| Remove arbitrary sleep | `tests/test_judge_coalesce.py` | `threading.Barrier` for batch contention instead of `time.sleep(0.05)` |
| Collection hygiene | `tests/conftest.py` | Ignore standalone harness scripts from pytest collection |
| Local recipes | `justfile` | `just test`, `just test-parallel`, `just test-profile`, `just test-all` |

Install dev deps (externally managed Python): `uv run --no-project --with-requirements requirements-dev.txt ...` or `pip install -r requirements-dev.txt` in a venv.

## Wait audit

Repo-wide search for `time.sleep` / `sleep(` under `tests/` found **zero** remaining arbitrary waits after the coalesce fix. Timeout parameters in hooks and gate scripts are bounded subprocess/socket budgets, not test pacing delays.

Audit verification is now executable:

```text
command: python3 scripts/audit_waits.py
result:
latency audit covered 47 grep-matched file(s)
documented decisions: 47 file(s)
test sleep calls: 0
```

`scripts/audit_waits.py` reruns the same file set used by the broad grep check, fails if a matched file is missing from this coverage ledger, fails if this doc lists stale coverage, fails if any matched file lacks a triage-table decision and review result, and fails if any `tests/` file contains a `sleep(` or `time.sleep` call.

| Matched files from the broad grep rerun | Decision | Review result |
|---|---|---|
| `tests/test_judge_coalesce.py` | Changed, then kept | The previous `time.sleep(0.05)` was removed. Remaining matches are bounded `threading.Event.wait(timeout=2.0)` and `threading.Barrier.wait(timeout=2.0)` used to make the lock-contention regression deterministic. |
| `tests/test_judge_daemon_routing.py` | Kept | Realtime daemon pool routing tests. Matches via bounded `submit(..., timeout=0.01)` overload assertions and idle-shutdown timing fixtures; uses `threading.Barrier` for burst-spread tests, not wall-clock sleeps. |
| `tests/test_posttool_parallel.py` | Kept | PostToolUse fan-out regression. Uses `threading.Barrier`/`threading.Event.wait(timeout=...)` to make the concurrency, budget-abandon, and coalesce assertions deterministic; contains no `time.sleep`. |
| `hooks/gate_post_tool.py`, `scripts/gate/posttool_judges.py`, `tests/test_posttool_timeout_budget.py` | Kept | PostToolUse concurrent judge fan-out: `posttool_judges.py` bounds the fan-out with `POSTTOOL_JUDGE_BUDGET` and daemon-thread `join(timeout)`, `gate_post_tool.py` wires it under the host PostToolUse timeout, and the budget test asserts the manifest/judge deadline relationships. Fail-open external-work bounds and timeout assertions, not pytest pacing. |
| `scripts/gate/breaker_state.py`, `scripts/gate/judge_client.py` | Kept | These are the only non-doc files still matched by the narrower sleep-only check. Both are bounded gate/judge polling loops, not pytest pacing. |
| `benchmark/bench.py` | Kept | Benchmark harness deadlines bound external CLI/terminal sessions and classify benchmark timeout outcomes. Removing them would make benchmark jobs hang-prone. |
| `hooks/test_after_edit.py` | Kept | Hook subprocess budget for automatic post-edit verification. Safety bound, not test pacing. |
| `hooks/gate_stop.py`, `scripts/gate/spec_io.py`, `scripts/gate/spec_validation.py`, `scripts/gate/spec_stop_validate.py`, `scripts/gate/codex_judge.py`, `scripts/gate/realtime_daemon.py`, `scripts/gate/judge_transport.py`, `scripts/gate/breaker_orchestration.py`, `scripts/gate/cli_install.py`, `scripts/gate/runtime_sync.py`, `scripts/gate/grade_override.py`, `scripts/generate_docs.py`, `scripts/shadow/outcome_collect.py` | Kept | Host hook, network, subprocess, generated-doc metadata, and fail-open budgets. These bound external work and are outside pytest scheduling. The spec/groundedness budgets now live in their split sub-modules (`spec_io`/`spec_validation`/`spec_stop_validate`, `breaker_orchestration`); the `spec.py` and `groundedness.py` facades carry no timing logic. |
| `tests/test_stop_timeout_budget.py`, `tests/test_runtime_sync.py`, `tests/test_test_after_edit.py`, `tests/test_loop_release.py`, `tests/test_grade_adjudicate_hook.py`, `tests/test_spec_state_notifications.py`, `tests/test_stop_codex_json.py`, `tests/test_judge_message_cap.py`, `tests/test_judge_runaway.py`, `tests/test_mcp_evidence.py`, `tests/test_completion_handoff.py`, `tests/test_auto_validate_stop.py`, `tests/test_supersession.py`, `tests/test_codex_judge_reask.py`, `tests/test_codex_judge_fragment.py` | Kept | Test fixtures and assertions for timeout behavior, monkeypatched timeout-aware APIs, or prose strings. The audit found no removable wall-clock sleeps in these tests. (`test_codex_judge_fragment.py` matches only via a no-op `settimeout` on an in-memory fake socket.) |
| `scripts/gate/db.py` | Kept | Consolidated SQLite gate store. Matches only via the bounded `PRAGMA busy_timeout`/`sqlite3.connect(timeout=...)` writer-lock budget (default 5000ms, `$UNIFABLE_DB_BUSY_TIMEOUT_MS`). This is a fail-open storage-contention bound, not pytest pacing; the expensive judge call is coalesced outside the module and never held inside a transaction. |
| `scripts/gate/breaker_judges.py` | Kept | Judge dispatch. Matches are bounded judge/network/subprocess deadlines, not test pacing. (The Realtime concurrency probe moved to `probes/bench_realtime_concurrency.py`, which is excluded from the audit scan.) |
| `scripts/gate/recon_lane.py` | Kept | gpt-realtime-mini recon/exec lane. Matches only via the bounded `UNIFABLE_RECON_CMD_TIMEOUT` validation-command deadline (passed to `run_check`) and the recon daemon request budget. Both are fail-open external-work bounds, not pytest pacing. |
| `scripts/gate/verify_lane.py`, `scripts/gate/breaker_runtime.py` | Kept | Breaker async auto-grounding lane. `verify_lane` bounds each background verification check with `UNIFABLE_VERIFY_CMD_TIMEOUT` (passed to `run_check`) and a 5s `git` state-fingerprint deadline; `breaker_runtime` adds `AUTO_VERIFY_WINDOW_SECONDS`, the wall-clock window that exempts an in-flight verification from the block-count fail-open cap (bounded by the verify timeout instead). Both are fail-open external-work bounds, not pytest pacing. |
| `scripts/gate/heavy_workflow.py` | Kept | HEAVY declare/execute phase helpers. Matches only via prose in a comment (`reset and wait` on a failed frontier); no `time.sleep`, subprocess timeout, or blocking wait API. |
| `scripts/gate/submit_enhance.py`, `tests/test_submit_enhance.py` | Kept | Repo-grounded prompt-enhance gate policy + its unit tests. The script matches via the bounded subprocess `timeout` passed to the Node enhancer (`UNIFABLE_PROMPT_ENHANCE_TIMEOUT_MS`, default 6000ms, fail-open to the static baseline on expiry); the test matches via `subprocess.TimeoutExpired` stubs and the timeout-env-knob assertions. No wall-clock sleeps. |
| `scripts/audit_waits.py` | Kept | Self-verifier for this audit. It contains the scan terms and coverage set so the raw grep command can be paired with a pass/fail accounting check. |
| `hooks/session_start.py`, `scripts/gate/janitor.py`, `scripts/gate/process_host.py`, `hooks/AGENTS.md` | Kept | SessionStart janitor dispatch. `process_host.py` matches via a bounded `ps` subprocess `timeout=3`; `janitor.py` matches via the bounded socket-connect probe (`settimeout`/`UNIFABLE_JANITOR_SOCKET_TIMEOUT`, default 0.2s) used to detect dead daemon sockets; `session_start.py` matches via prose ("host's 30s timeout") only -- it does no waiting itself. The reaper is detached/fire-and-forget and never on the pytest path. `hooks/AGENTS.md` is prose-only. No `time.sleep`. |
| `docs/testing-optimization.md` | Kept | Documentation/prose matches only. |

No remaining broad-grep match is a realistic runtime optimization target. The suite-level runtime improvement came from parallel scheduling and overlapping independent harnesses.

## Measurements (median of 3 runs)

| Suite | Before (serial) | After | Change |
|---|---:|---:|---|
| Pytest (851 tests) | 65.5s (`-n 0`) | 11.5s (parallel default) | **-82%** (~5.7x) |
| `eval_gate_proof.py` (39 scenarios) | 29.3s | 3.5s | **-88%** (~8.4x) |
| `test_gate_robustness.py` | 15.4s | 15.4s | unchanged (already subprocess-bound; overlaps with pytest) |
| **Full commit path** (`scripts/run_tests.sh`) | ~109s sequential sum | **15.2s** wall clock | **-86%** (~7.2x) |

Raw serial pytest runs: 58.7s, 69.6s, 65.5s wall. Raw parallel pytest: 16.4s, 11.5s, 8.4s. Raw eval after parallelization: 3.5s, 5.4s, 2.9s. Raw `run_tests.sh`: 15.5s, 15.2s, 13.9s.

Slowest serial pytest tests (unchanged per-test cost; parallelism hides aggregate wait):

```text
tests/test_breaker_keying.py::test_breaker_blocks_until_validated   ~3.7s
tests/test_breaker_keying.py::test_session_keying_one_spec_per_session
tests/test_spec_gate.py integration subprocess tests                ~1.9-2.4s each
```

Isolation smoke: `pytest -n auto --random-order` — **851 passed** in 8.2s.

## Deliberately not pursued

- **In-process hook calls** instead of subprocess integration tests — would speed up tests but weakens the “real hook on stdin” guarantee these suites enforce.
- **`coverage.py sysmon`** — no coverage gate in the commit path today.
- **Migrating `test_gate_robustness.py` into pytest** — low payoff (~15s) vs. risk to the standalone safety harness shape.
- **Permanent `--random-order` in CI** — used once for xdist bring-up only; kept out of default `addopts` to avoid noise.

## How to run

```bash
just test-all          # commit.sh parity: pytest + eval + robustness (parallel jobs)
just wait-audit        # verify every broad wait/timeout grep match is documented
just test-parallel     # pytest only, all cores
just test              # pytest serial (-n 0) for debugging
just test-profile      # serial + --durations=20
PYTEST_SERIAL=1 bash scripts/run_tests.sh   # full suite with serial pytest
TEST_TIMING=1 bash scripts/run_tests.sh     # print /usr/bin/time for each job
```

Debug a single failure with serial pytest (`-n 0`) because pytest-xdist limits stdout aggregation and `--pdb` under workers.

## Critic verdict

**APPROVED** (readonly critic agent, 2026-06-24).

- Parallelize-first strategy matches pytest-xdist and Trail of Bits guidance; subprocess hook fidelity preserved.
- No unjustified sleeps remain; coalesce batch contention uses `threading.Barrier`.
- Wall-clock improvements are substantiated on this machine (median of 3 runs).
- `--random-order` + xdist smoke passed (851 tests).

**Residual risks (accepted):**

- Speedup scales with CPU count; low-core CI runners will see smaller pytest gains. Full-path wall time stays bounded by `test_gate_robustness.py` (~15s) when jobs overlap.
- `scripts/run_tests.sh` and `scripts/commit.sh` assume dev deps are installed (`pip install -r requirements-dev.txt` in a venv, or `uv run --with-requirements requirements-dev.txt` as in `just test-all`).
- Some tests mutate `os.environ` directly; random-order smoke did not surface ordering bugs. Prefer `monkeypatch.setenv` in future tests.

**No further realistic optimizations** without abandoning subprocess integration coverage or rewriting slow hook chains in-process.
