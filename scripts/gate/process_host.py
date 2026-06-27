#!/usr/bin/env python3
"""Find the agent host process for the current hook invocation.

The janitor's safety property is "never reap state for a session whose host
process is still alive." Hosts (Claude Code, Codex, GLM, Cursor) do NOT put the
session id in their argv, so ``pgrep -f <session_id>`` is useless. The one
robust signal is the host PID: the hook process is a descendant of the host, so
walking process ancestry from ``os.getpid()`` up to PID 1 reaches the host.

``find_host_ancestor`` returns ``(pid, comm)`` for the first ancestor whose
command matches a known host, or ``None``. The janitor records that PID in the
alive-registry and later probes it with ``os.kill(pid, 0)`` plus a comm match
(the comm match defends against PID reuse: a dead host's PID can be recycled by
an unrelated process).

Stdlib only; host-agnostic. The ``ps`` lookup is injected so tests can stub the
process tree without spawning ``ps``.

FAIL-OPEN: any error returns ``None`` -- a missing host PID means the
alive-marker carries no liveness claim, and the janitor treats that session as
not-protected-by-liveness (cleaned by age). That is the safe direction: a
marker we cannot interpret protects nothing, and age-based reaping still runs.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable

# Basename substrings (case-insensitive) that identify a host process by comm.
# "cursor" matches "Cursor Helper" / "Cursor" etc. "node" alone is too broad
# (any node tool would match), so node is only accepted when its full command
# line mentions "claude" or "codex" (the Claude Code / Codex CLIs run as node).
_HOST_COMM_SUBSTRINGS = ("claude", "codex", "glm", "cursor")
_NODE_HOST_HINTS = ("claude", "codex")


def _default_ps_line(pid: int) -> tuple[int, str, str] | None:
    """Return ``(ppid, comm_basename, full_command)`` for *pid*, or None.

    Uses ``ps -p <pid> -o ppid=,comm=,command=``. ``comm=`` is the truncated
    basename; ``command=`` is the full argv (lets us sniff a node-launched
    host). Fails open on any OS/parse error.
    """
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=,comm=,command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip()
    if not line:
        return None
    # ps separates fields with whitespace; command= preserves the argv spacing.
    parts = line.split(None, 2)
    if len(parts) < 2:
        return None
    try:
        ppid = int(parts[0])
    except ValueError:
        return None
    comm = parts[1]
    command = parts[2] if len(parts) > 2 else comm
    return ppid, comm, command


def _is_host(comm: str, command: str) -> bool:
    """True when this process looks like a supported agent host."""
    comm_lc = comm.lower()
    if any(h in comm_lc for h in _HOST_COMM_SUBSTRINGS):
        return True
    if comm_lc.endswith("node") or os.path.basename(comm_lc) == "node":
        cmd_lc = command.lower()
        return any(h in cmd_lc for h in _NODE_HOST_HINTS)
    return False


def find_host_ancestor(
    start_pid: int | None = None,
    *,
    max_hops: int = 64,
    ps_provider: Callable[[int], tuple[int, str, str] | None] = _default_ps_line,
) -> tuple[int, str] | None:
    """Walk process ancestry from *start_pid* up to PID 1; return the first host.

    Returns ``(host_pid, host_comm)`` for the nearest ancestor whose comm/command
    identifies a supported host, or ``None`` if none is found within *max_hops*.
    Never raises.
    """
    pid = int(start_pid) if start_pid is not None else os.getpid()
    seen: set[int] = set()
    for _ in range(max_hops):
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        line = ps_provider(pid)
        if line is None:
            return None
        ppid, comm, command = line
        if _is_host(comm, command):
            return pid, comm
        if ppid <= 1:
            return None
        pid = ppid
    return None
