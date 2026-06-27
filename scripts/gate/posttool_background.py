#!/usr/bin/env python3
"""Fire-and-forget background reconcile/discover for PostToolUse.

The reconcile + frontier-discover judges are ADVISORY board maintenance: they
gate nothing, so they must not block the agent's next tool behind a
gpt-realtime-2 round-trip (the old design awaited them inline under the host
PostToolUse timeout, on EVERY evidence-changing tool). Here `gate_post_tool`
spawns this module detached (`start_new_session`, the janitor pattern); the
child re-derives the spec/ledger, runs the judges, applies the deltas to one
base spec under `update_spec`'s lock, and ENQUEUES the resulting "Spec update"
context (`db.posttool_bg_push`) for the NEXT PreToolUse to drain and inject.

So reconcile lands one tool-step late instead of blocking the hot path, and the
churn guards in spec_judge/posttool_notify keep the enqueued context from
repeating. Two hard rules (prime directive):

  - Detached child only. The parent spawns and returns immediately; a slow or
    hung judge never delays the hook or the host.
  - Spawn debounce. `db.posttool_bg_lease` gates one in-flight job per spec_key
    per TTL window, so sequential evidence-changing tools cannot fork a process
    storm.

Fail-open everywhere: any error spawns nothing / pushes nothing / drains "".

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


# In-flight lease window: at most one background reconcile job per spec_key per
# this many seconds. Comfortably covers one judge round-trip so sequential tools
# coalesce onto the running job instead of spawning a new one each time.
POSTTOOL_BG_LEASE_TTL = _env_float("UNIFABLE_POSTTOOL_BG_TTL", 90.0)


def _spec_key_for(input_data: dict) -> str:
    from spec_io import _spec_key, canonical_project_root, resolve_session_id

    cwd = canonical_project_root(input_data.get("cwd") or os.getcwd())
    return _spec_key(cwd, resolve_session_id(input_data, default=None))


def drain_pending_context(input_data: dict) -> str:
    """Read-and-clear any completed background reconcile context for this session's
    spec. Called from PreToolUse so the delta surfaces on the next gated tool after
    the job finished. Fail-open: returns "" on any error."""
    try:
        import db

        return db.posttool_bg_drain(_spec_key_for(input_data))
    except Exception:
        return ""


def _spawn_enabled() -> bool:
    return (os.environ.get("UNIFABLE_POSTTOOL_BG", "1") or "1").strip().lower() not in ("0", "false", "no", "off")


def spawn_reconcile_job(input_data: dict, *, want_reconcile: bool, want_discover: bool) -> bool:
    """Lease + spawn the detached background reconcile/discover job.

    Returns True when a child was spawned, False when nothing was requested, the
    lease is held by an in-flight job, spawning is disabled (UNIFABLE_POSTTOOL_BG=0),
    or the spawn failed (all fail-open). The child reads *input_data* (plus the two
    flags) from a temp file it deletes."""
    if not (want_reconcile or want_discover):
        return False
    if not _spawn_enabled():
        return False
    try:
        import db

        spec_key = _spec_key_for(input_data)
        if not db.posttool_bg_lease(spec_key, time.time(), POSTTOOL_BG_LEASE_TTL):
            return False
    except Exception:
        return False

    payload = {
        "input_data": input_data,
        "want_reconcile": bool(want_reconcile),
        "want_discover": bool(want_discover),
    }
    try:
        fd, tmp = tempfile.mkstemp(prefix="unifable-bg-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
    except OSError:
        return False

    try:
        devnull = open(os.devnull, "wb")
    except OSError:
        devnull = None
    try:
        subprocess.Popen(
            [sys.executable, str(_HERE / "posttool_background.py"), "--run", tmp],
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

            db.posttool_bg_push(_spec_key_for(input_data), "")
        except Exception:
            pass
        with __import__("contextlib").suppress(OSError):
            os.unlink(tmp)
        return False


def _build_context(merged: dict, headlines: list, added: list) -> str:
    from spec_judge import build_spec_update_context

    discovery_context = build_spec_update_context(headlines) if headlines else ""
    if added:
        try:
            from heavy_workflow import format_approach_board

            ids = ", ".join(t["id"] for t in added)
            frontier_context = (
                "Spec update:\n"
                f"Judge added frontier approach(s): {ids}. Explore ALL frontiers"
                " thoroughly (check each one). The judge compares evidence on Stop"
                " and may adopt the best over primary.\n" + format_approach_board(merged)
            )
            discovery_context = discovery_context + "\n" + frontier_context if discovery_context else frontier_context
        except Exception:
            pass
    return discovery_context


def run_reconcile_job(input_data: dict, *, want_reconcile: bool, want_discover: bool) -> str:
    """Compute reconcile/discover, apply the deltas under the spec lock, enqueue the
    resulting context, and return it (for tests). Fail-open: returns "" on any error."""
    try:
        from judge_transport import bind_session

        bind_session(input_data)
    except Exception:
        pass
    try:
        import db
        from citations import activity_from_ledger
        from ledger import load_ledger
        from spec_io import canonical_project_root, load_spec, resolve_session_id, update_spec
        from spec_judge import (
            apply_frontier_additions,
            apply_reconcile_actions,
            compute_frontier_additions,
            compute_reconcile_actions,
        )

        cwd = str(canonical_project_root(input_data.get("cwd") or os.getcwd()))
        task_id = resolve_session_id(input_data, default=None)
        spec = load_spec(cwd, task_id) if task_id else None
        if not task_id or not spec:
            return ""
        activity = activity_from_ledger(load_ledger(input_data))
        transcript_path = str(input_data.get("transcript_path") or "") or None

        reconcile_actions: list = (
            compute_reconcile_actions(spec, activity, transcript_path=transcript_path) if want_reconcile else []
        )
        frontier_additions: list = compute_frontier_additions(spec, activity) if want_discover else []
        if not (reconcile_actions or frontier_additions):
            db.posttool_bg_push(_spec_key_for(input_data), "")  # clear lease
            return ""

        captured: dict[str, list] = {"headlines": [], "added": []}

        def merge(base):
            if reconcile_actions:
                captured["headlines"] = apply_reconcile_actions(base, reconcile_actions, evidence=activity)
            if frontier_additions:
                captured["added"] = apply_frontier_additions(base, frontier_additions)

        merged = update_spec(cwd, task_id, merge)
        if merged is None:
            db.posttool_bg_push(_spec_key_for(input_data), "")
            return ""

        added = captured["added"]
        context = _build_context(merged, captured["headlines"], added)
        if added:
            try:
                from ledger import ledger_key

                db.frontier_bump_discovery(ledger_key(input_data))
            except Exception:
                pass
        db.posttool_bg_push(_spec_key_for(input_data), context)
        return context
    except Exception:
        try:
            import db

            db.posttool_bg_push(_spec_key_for(input_data), "")  # release lease, fail-open
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
            run_reconcile_job(
                payload.get("input_data") or {},
                want_reconcile=bool(payload.get("want_reconcile")),
                want_discover=bool(payload.get("want_discover")),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
