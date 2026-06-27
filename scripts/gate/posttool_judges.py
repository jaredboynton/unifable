#!/usr/bin/env python3
"""PostToolUse judge fan-out (host-agnostic, fail-open).

`gate_post_tool.py` may need up to four gpt-realtime-2 judge round-trips on one
tool result: reconcile the task board, discover frontier approaches, release the
groundedness breaker, and offer a repeated-failure hint. Run straight-line they
serialize four ~90s deadlines under a 10s host timeout. They all route through
the warm per-session daemon (`judge_transport.ask_structured`), so this module
fans the independent ones out concurrently under a single wall-clock budget.

Two hard rules, both from the prime directive:

  - Daemon threads only. A judge that hangs past the budget is ABANDONED, not
    awaited -- the hook returns and the process exits regardless. A non-daemon
    pool (e.g. ThreadPoolExecutor) would join stuck workers on shutdown and wedge
    the hook, which is exactly the failure we are removing.
  - No spec/breaker mutation here. Jobs are zero-arg thunks the caller builds; the
    caller applies the returned deltas to one base spec under its own lock. This
    keeps the package host-agnostic and the merge deterministic.

Fail-open everywhere: a thunk that raises, or one still running at the deadline,
simply contributes no result (empty delta / no message). Nothing here can block.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# Wall-clock budget for the whole fan-out, kept under the host PostToolUse timeout
# (hooks.json wires gate_post_tool at 120s). Because the jobs run concurrently the
# wall clock is ~one judge round-trip, not the sum, so this comfortably covers a
# single slow judge while leaving host margin for teardown. Mirrors the Stop
# hook's UNIFABLE_STOP_BUDGET. See tests/test_posttool_timeout_budget.py.
POSTTOOL_JUDGE_BUDGET = _env_float("UNIFABLE_POSTTOOL_BUDGET", 100.0)

# Coalesce window for session-level spec judging across a parallel tool batch.
# Matches the breaker's UNIFABLE_JUDGE_COALESCE_WINDOW default (2s): sibling
# PostToolUse processes of one batch fire within milliseconds, so the first to
# claim runs reconcile+discover and the rest skip; genuinely later sequential
# evidence (> window apart) still re-runs reconcile.
POSTTOOL_COALESCE_WINDOW = _env_float("UNIFABLE_POSTTOOL_COALESCE_WINDOW", 2.0)


def _transcript_turn_marker(path: str | os.PathLike[str] | None) -> str:
    """Non-empty line count in the transcript file, or "" when unavailable.

    Appended to the prompt fingerprint so two turns with identical user text still
    get distinct epochs once the transcript grows."""
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        with p.open(encoding="utf-8", errors="replace") as handle:
            return str(sum(1 for line in handle if line.strip()))
    except OSError:
        return ""


def _turn_epoch(input_data: dict[str, Any]) -> str:
    """Stable fingerprint of the current assistant turn (latest user prompt + depth).

    Used to coalesce sibling PostToolUse processes within one parallel tool batch.
    Returns "" when the transcript is missing, unreadable, or has no human turn --
    callers MUST treat that as an unreliable epoch and fail open to running judges
    rather than suppressing work."""
    try:
        from transcript_tail import latest_user_prompt_fingerprint

        transcript_path = input_data.get("transcript_path") or None
        fp = latest_user_prompt_fingerprint(transcript_path) or ""
        if not fp:
            return ""
        marker = _transcript_turn_marker(transcript_path)
        if not marker:
            return ""
        return f"{fp}:{marker}"
    except Exception:
        return ""


def claim_spec_judging(
    input_data: dict[str, Any],
    *,
    now: float | None = None,
    window: float = POSTTOOL_COALESCE_WINDOW,
) -> bool:
    """Cross-process claim for the session-level spec judges (reconcile+discover).

    Returns True when THIS process should run them, False when a sibling in the same
    parallel tool batch already claimed within the coalesce window of this turn (so
    this process skips the redundant judges). Delegates to ``db.posttool_spec_claim``,
    an atomic compare-and-set on a dedicated table keyed by the SAME spec key the spec
    itself uses (``_spec_key`` over ``resolve_session_id``) -- so the claim cannot be
    clobbered by a concurrent ledger write and cannot key-mismatch the spec it guards.

    When no reliable turn epoch exists (missing/unreadable transcript, no human turn),
    coalescing is skipped and this returns True so judges still run. Disarm and hint
    are tool/failure-specific and are NOT coalesced -- only spec judging is.
    Fail-open: any error returns True (do the work rather than skip)."""
    ts = time.time() if now is None else now
    epoch = _turn_epoch(input_data)
    if not epoch:
        return True
    try:
        import db
        from spec_io import _spec_key, canonical_project_root, resolve_session_id

        cwd = canonical_project_root(input_data.get("cwd") or os.getcwd())
        spec_key = _spec_key(cwd, resolve_session_id(input_data, default=None))
        return db.posttool_spec_claim(spec_key, ts, epoch, window)
    except Exception:
        return True


def run_judges_parallel(
    jobs: dict[str, Callable[[], Any]],
    *,
    budget: float = POSTTOOL_JUDGE_BUDGET,
) -> dict[str, Any]:
    """Run named zero-arg jobs concurrently on daemon threads, bounded by *budget*.

    Returns ``{name: result}`` for every job that finished within the budget; jobs
    that raised or were still running at the deadline are simply absent (fail-open,
    never raises). The deadline bounds how long we WAIT -- abandoned daemon threads
    keep running harmlessly and never delay the caller or process exit."""
    if not jobs:
        return {}
    results: dict[str, Any] = {}
    lock = threading.Lock()

    def runner(name: str, fn: Callable[[], Any]) -> None:
        try:
            value = fn()
        except Exception:
            return  # fail-open: a failed judge contributes no result
        with lock:
            results[name] = value

    threads: list[threading.Thread] = []
    for name, fn in jobs.items():
        thread = threading.Thread(
            target=runner,
            args=(name, fn),
            name=f"posttool-judge-{name}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    deadline = time.monotonic() + max(0.0, budget)
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))

    with lock:
        return dict(results)


@dataclass
class PosttoolResult:
    """Deltas + messages from one PostToolUse judge fan-out.

    ``reconcile_actions`` and ``frontier_additions`` are pure deltas the caller
    applies to one base spec under lock; ``disarm_message`` and ``hint_text`` are
    advisory context strings. ``completed`` records which jobs finished within the
    budget (for observability/tests); a missing job means it was abandoned or had
    nothing to say."""

    reconcile_actions: list[dict[str, Any]] = field(default_factory=list)
    frontier_additions: list[dict[str, Any]] = field(default_factory=list)
    disarm_message: str = ""
    hint_text: str = ""
    completed: tuple[str, ...] = ()


def run_posttool_judges(
    *,
    reconcile: Callable[[], list[dict[str, Any]]] | None = None,
    discover: Callable[[], list[dict[str, Any]]] | None = None,
    disarm: Callable[[], str] | None = None,
    hint: Callable[[], str] | None = None,
    budget: float = POSTTOOL_JUDGE_BUDGET,
) -> PosttoolResult:
    """Fan out the selected PostToolUse judges concurrently under *budget*.

    Each argument is an optional zero-arg thunk the caller builds (closing over the
    spec snapshot, activity, breaker state, etc.). ``reconcile``/``discover`` return
    deltas; ``disarm``/``hint`` return context strings. Pass only the jobs the
    hook's gating selected; the rest stay None and are skipped."""
    jobs: dict[str, Callable[[], Any]] = {}
    if reconcile is not None:
        jobs["reconcile"] = reconcile
    if discover is not None:
        jobs["discover"] = discover
    if disarm is not None:
        jobs["disarm"] = disarm
    if hint is not None:
        jobs["hint"] = hint

    raw = run_judges_parallel(jobs, budget=budget)

    reconcile_actions = raw.get("reconcile") or []
    frontier_additions = raw.get("discover") or []
    return PosttoolResult(
        reconcile_actions=reconcile_actions if isinstance(reconcile_actions, list) else [],
        frontier_additions=frontier_additions if isinstance(frontier_additions, list) else [],
        disarm_message=str(raw.get("disarm") or ""),
        hint_text=str(raw.get("hint") or ""),
        completed=tuple(raw.keys()),
    )
