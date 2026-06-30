#!/usr/bin/env python3
"""SessionStart hook: refresh the stable ~/.unifable runtime, register the
session as alive, and inject the standing operating-mode context.

Three jobs, all fail-open (a hook bug must never stop a session from starting):

1. Runtime sync: copy the newest cached plugin version into ~/.unifable and
   atomically flip ~/.unifable/current, so hooks never exec from a versioned
   cache dir the host marketplace may delete (the exit-127 dangle bug). Then a
   version-aware heal (cli_install.ensure_cli) resolves the effective plugin
   root and re-seeds from it when ~/.unifable/current is missing, broken, or
   older than the loaded plugin, so the global launchers (unifable, unifusion,
   unitrace, unisearch) always resolve on PATH.
2. Janitor dispatch: write an alive-marker for THIS session
   (~/.unifable/alive/<skey>.json carrying the host PID) so the reaper never
   cleans a session whose host process is still alive, then -- throttled to at
   most once per UNIFABLE_JANITOR_INTERVAL_S -- spawn scripts/gate/janitor.py
   detached (start_new_session) to reap stale state. The marker write is tiny
   and synchronous; the sweep runs in a child past the host's 30s timeout.
3. Context injection: emit the operating-mode block via SessionStart
   additionalContext. This replaces the old static CLAUDE.md/AGENTS.md block
   injection -- the posture now ships only when the plugin is enabled, and is
   not duplicated into host memory files that other CLI tools also read.

Emits {} on any internal error; never blocks.

# cleanup-traps: not-applicable -- the janitor is spawned detached (start_new_session) to outlive this hook; no parent-child lifetime to manage or reap.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime


def _read_stdin_json() -> dict:
    """Read the host's SessionStart JSON payload. Non-blocking when no stdin is
    piped (interactive/CI run): a TTY has nothing to read, so return {}."""
    try:
        if sys.stdin.isatty():
            return {}
    except (OSError, ValueError):
        return {}
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _janitor_enabled() -> bool:
    return (os.environ.get("UNIFABLE_JANITOR", "1") or "1").strip().lower() not in ("0", "false", "no", "off")


def _dispatch_janitor(input_data: dict, here: str) -> None:
    """Write this session's alive-marker, then spawn the reaper if not throttled.

    The marker is written even when the janitor is disabled: it is the safety
    signal that lets any *enabled* janitor (from another session) spare this
    session. The detached spawn is skipped when disabled or within the throttle
    window. Never raises.
    """
    import atomicio
    import ledger
    import process_host
    from spec_io import canonical_project_root, resolve_session_id

    cwd = input_data.get("cwd") or os.getcwd()
    skey = ledger.ledger_key(input_data)
    root = canonical_project_root(cwd)
    sid = resolve_session_id(input_data, default="") or ""
    host = process_host.find_host_ancestor()
    host_pid = host[0] if host else 0
    host_comm = host[1] if host else ""

    alive_dir = ledger.data_root() / "alive"
    marker = {
        "skey": skey,
        "session_id": sid,
        "project_root": str(root),
        "host_pid": host_pid,
        "host_comm": host_comm,
        "started_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }
    try:
        atomicio.write_text_atomic(alive_dir / f"{skey}.json", json.dumps(marker, ensure_ascii=False))
    except Exception:
        pass

    if not _janitor_enabled():
        return
    sentinel = alive_dir / ".last_sweep"
    try:
        if sentinel.is_file() and (time.time() - sentinel.stat().st_mtime) < _env_int("UNIFABLE_JANITOR_INTERVAL_S", 3600):
            return
    except OSError:
        pass

    gate_dir = os.path.join(here, "..", "scripts", "gate")
    janitor = os.path.join(gate_dir, "janitor.py")
    try:
        devnull = open(os.devnull, "wb")
    except OSError:
        devnull = None
    try:
        subprocess.Popen(
            [sys.executable, janitor, "--run"],
            stdin=subprocess.DEVNULL,
            stdout=devnull or subprocess.DEVNULL,
            stderr=devnull or subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            cwd=str(gate_dir),
        )
    except Exception:
        pass


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "scripts", "gate"))
    try:
        import runtime_sync

        runtime_sync.sync_runtime()
    except Exception:
        pass

    # Version-aware ping + heal: resolve the effective plugin root (host env then
    # latest cache semver) and re-seed ~/.unifable from it when current is missing,
    # broken, or older than the loaded plugin. The cache-scan sync above advances
    # current to the newest cache version; this guarantees the actually-loaded
    # plugin is the one installed, so the global launchers (unifable, unifusion,
    # unitrace, unisearch) always resolve on PATH. Probe-only when current; spawns
    # the re-seed subprocess only when needs_heal is True. Fail-open, opt-out via
    # UNIFABLE_CLI_AUTO_HEAL=0.
    try:
        import cli_install

        cli_install.ensure_cli()
    except Exception:
        pass

    input_data: dict = {}
    try:
        input_data = _read_stdin_json()
    except Exception:
        input_data = {}

    try:
        _dispatch_janitor(input_data, here)
    except Exception:
        pass

    payload: dict = {}
    try:
        from context_block import build_session_payload

        payload = build_session_payload()
    except Exception:
        payload = {}

    # Record that the standing first-action frame fired, so the first-prompt
    # scaffold onboarding (gate_prompt.py) does not re-emit the "unifable restate"
    # instruction. Fail-open: a marker-write bug never blocks session start.
    if payload:
        try:
            import ledger as _ledger

            def _mark_frame(led):
                led["session_frame_notified"] = True

            _ledger.update_ledger(input_data, _mark_frame)
        except Exception:
            pass

    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
