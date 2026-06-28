#!/usr/bin/env python3
"""Fire-and-forget background breaker-release (disarm) for PostToolUse.

The groundedness breaker ARMS synchronously in PreToolUse (it must block a
mutation before it runs). The LIFT (disarm) is the opposite: it only ever removes
a block, so it must not sit on the PostToolUse hot path spending a gpt-realtime-2
release-judge round-trip on every release tool. Here `gate_post_tool` spawns this
module detached (`start_new_session`, the verify_lane / posttool_background
pattern); the child runs the transcript release judge under `breaker_lock`,
persists the disarmed state, and ENQUEUES the disarm message (`db.breaker_release_push`)
for the NEXT PreToolUse to drain and surface (or for Stop to drain on a text-only
tail).

Why this is correct (the convergence invariants):

  - Arming stays synchronous in PreToolUse; only the lift moves here.
  - One-writer discipline: the worker takes the SAME `breaker_lock` flock as the
    foreground arm/disarm, so an async disarm can never clobber a concurrent arm.
    (The old inline PostToolUse disarm wrote breaker state WITHOUT the lock; this
    closes that race.)
  - Disarm is idempotent across callers: PreToolUse re-runs the release judge on
    every armed call, so a slow/dead worker is harmless -- the next gated tool
    disarms itself. The worker just makes it usually-already-done.
  - Lease debounce: `db.breaker_release_lease` gates one in-flight disarm per
    breaker (rel_key = ledger_key) per TTL, so a burst of release tools cannot
    fork a process storm.

Fail-open everywhere: any error spawns nothing / pushes nothing / drains "". A
disarm that never lands leaves the breaker armed, and PreToolUse / Stop converge
it -- the safe direction.

# cleanup-traps: not-applicable -- the job is spawned detached (start_new_session)
# to outlive the hook; there is no parent-child lifetime to manage or reap.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# In-flight lease window: at most one background disarm job per breaker per this
# many seconds. Comfortably covers one release-judge round-trip so a burst of
# release tools coalesces onto the running job instead of spawning a new one each.
BREAKER_RELEASE_LEASE_TTL = _env_float("UNIFABLE_BREAKER_RELEASE_TTL", 90.0)


def _rel_key(input_data: dict) -> str:
    """Session-stable queue/lease key for the disarm lane. Uses ledger_key (the
    same key breaker state is stored under), so PreToolUse and Stop can drain
    without recomputing the task lineage. Fail-open to ''."""
    try:
        from ledger import ledger_key

        return ledger_key(input_data)
    except Exception:
        return ""


def drain_pending_release(input_data: dict) -> str:
    """Read-and-clear any completed background disarm message for this session's
    breaker. Called from PreToolUse (and Stop) so the lift surfaces on the next
    gated tool after the worker finished. Fail-open: returns "" on any error."""
    try:
        import db

        key = _rel_key(input_data)
        if not key:
            return ""
        return db.breaker_release_drain(key)
    except Exception:
        return ""


def _spawn_enabled() -> bool:
    return (os.environ.get("UNIFABLE_BREAKER_RELEASE_BG", "1") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def spawn_release_job(input_data: dict, tool_name: str, fresh_tool: str) -> bool:
    """Lease + spawn the detached background disarm job.

    Returns True when a child was spawned, False when the lease is held by an
    in-flight job, spawning is disabled (UNIFABLE_BREAKER_RELEASE_BG=0), or the
    spawn failed (all fail-open). The child reads *input_data* plus the fresh tool
    block from a temp file it deletes."""
    if not _spawn_enabled():
        return False
    key = _rel_key(input_data)
    if not key:
        return False
    try:
        import db

        if not db.breaker_release_lease(key, time.time(), BREAKER_RELEASE_LEASE_TTL):
            return False
    except Exception:
        return False

    payload = {
        "input_data": input_data,
        "tool_name": str(tool_name or ""),
        "fresh_tool": str(fresh_tool or ""),
    }
    try:
        fd, tmp = tempfile.mkstemp(prefix="unifable-release-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except OSError:
        # Could not stage payload: release the lease so a later tool retries.
        try:
            import db

            db.breaker_release_push(key, "")
        except Exception:
            pass
        return False

    try:
        devnull = open(os.devnull, "wb")
    except OSError:
        devnull = None
    try:
        subprocess.Popen(
            [sys.executable, str(_HERE / "breaker_release_lane.py"), "--run", tmp],
            stdin=subprocess.DEVNULL,
            stdout=devnull or subprocess.DEVNULL,
            stderr=devnull or subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd=str(_HERE),
        )
        return True
    except Exception:
        # Spawn failed: clear the lease and drop the temp file so a later tool retries.
        try:
            import db

            db.breaker_release_push(key, "")
        except Exception:
            pass
        with __import__("contextlib").suppress(OSError):
            os.unlink(tmp)
        return False


def run_release_job(input_data: dict, tool_name: str, fresh_tool: str) -> str:
    """Run the transcript release judge under breaker_lock, persist the disarm, and
    enqueue the resulting message. Returns it (for tests). Fail-open: returns "" on
    any error, always releasing the lease so a later tool can retry."""
    key = _rel_key(input_data)
    try:
        from judge_transport import bind_session

        bind_session(input_data)
    except Exception:
        pass
    try:
        from breaker_orchestration import evaluate_post_tool_release
        from breaker_state import breaker_lock, load_breaker, save_breaker
        from ledger import load_ledger

        active_task = ""
        try:
            active_task = str((load_ledger(input_data) or {}).get("active_task") or "")
        except Exception:
            active_task = ""

        with breaker_lock(input_data):
            breaker = load_breaker(input_data)
            if not breaker.get("breaker_armed") and not breaker.get("breaker_provisional"):
                if key:
                    import db

                    db.breaker_release_push(key, "")  # release lease, nothing to do
                return ""
            _grounded, _needed, message = evaluate_post_tool_release(
                input_data, breaker, fresh_tool=fresh_tool, active_task=active_task
            )
            save_breaker(input_data, breaker)
        if key:
            import db

            db.breaker_release_push(key, str(message or ""))
        return str(message or "")
    except Exception:
        try:
            if key:
                import db

                db.breaker_release_push(key, "")  # release lease, fail-open
        except Exception:
            pass
        return ""


def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "--run":
        tmp = argv[1]
        try:
            with open(tmp, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            payload = {}
        finally:
            with __import__("contextlib").suppress(OSError):
                os.unlink(tmp)
        if isinstance(payload, dict):
            run_release_job(
                payload.get("input_data") or {},
                str(payload.get("tool_name") or ""),
                str(payload.get("fresh_tool") or ""),
            )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
