#!/usr/bin/env python3
"""Atomic, concurrency-safe writes for the gate's small JSON state files.

The gate hooks (UserPromptSubmit, PostToolUse, Stop) can run concurrently on the
same session: a single assistant turn may issue several tool calls in parallel,
firing several PostToolUse hooks at once, each loading-modifying-saving the same
per-session ledger. A writer that names its temp file deterministically
(``path + ".tmp"``) then races — the first ``os.replace`` consumes the shared
temp and the second raises::

    [Errno 2] No such file or directory: '<id>.tmp' -> '<id>.json'

``write_text_atomic`` gives every writer a UNIQUE temp file in the destination's
own directory (so ``os.replace`` stays atomic — same filesystem). Concurrent
writers never share a temp name, the rename is last-writer-wins, and a missing
parent directory is created first. This is the single hardened write path the
gate's JSON state modules (ledger, findings, spec) share.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

# Temp files orphaned by a hard kill (SIGKILL / OOM / power loss between mkstemp
# and replace) are reaped after this many seconds. Loaders only read the real
# ".json" name, so without this sweep the unique-temp scheme would leak one tiny
# file per hard-kill forever.
_ORPHAN_TEMP_MAX_AGE_S = 300.0


def _sweep_orphan_temps(parent: Path, basename: str) -> None:
    """Best-effort removal of this file's stale ``<basename>.<rand>.tmp`` orphans.

    Targeted (only this destination's temps), age-bounded, and never raises — a
    sweep failure must not break the write that just succeeded.
    """
    prefix = basename + "."
    try:
        now = time.time()
        with os.scandir(parent) as entries:
            for entry in entries:
                name = entry.name
                if not (name.startswith(prefix) and name.endswith(".tmp")):
                    continue
                try:
                    if now - entry.stat().st_mtime > _ORPHAN_TEMP_MAX_AGE_S:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def write_text_atomic(path: str | os.PathLike[str], text: str, encoding: str = "utf-8") -> Path:
    """Write *text* to *path* atomically, creating parent dirs as needed.

    Safe under concurrent writers to the same path: each call uses its own temp
    file, so the rename never hits ENOENT on a temp another writer already moved.
    Raises only on a genuine filesystem error (and never leaks the temp file).
    """
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Unique temp in the destination's own directory keeps os.replace atomic
    # (same filesystem) and prevents the fixed-".tmp" name race.
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        # Never leave the unique temp behind on failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _sweep_orphan_temps(parent, path.name)
    return path
