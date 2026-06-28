#!/usr/bin/env python3
"""Fire-and-forget reaper for stale ~/.unifable/ state. Invoked detached by the
SessionStart hook (never on the hook's hot path; runs past the host's 30s
timeout via ``start_new_session``).

SAFETY CONTRACT (sacred): never reap state for a session whose host process is
still alive. The SessionStart hook writes one alive-marker per session to
``<data_root>/alive/<skey>.json`` carrying the host PID (resolved via
``process_host.find_host_ancestor``). Before reaping any skey-keyed entry we
probe ``os.kill(host_pid, 0)`` + a comm match; a live host -> SKIP every entry
for that skey (and, for spec-keyed state, every entry sharing its dir_hash).
See ``is_marker_live``.

FAIL-OPEN CONTRACT (sacred, per AGENTS.md): every stage is wrapped in
try/except and never raises into the caller. A janitor bug must never wedge a
session or corrupt state. Bounded by ``UNIFABLE_JANITOR_MAX_REAP`` so a
pathological tree cannot run unbounded.

Reap targets + rules (age = ``UNIFABLE_JANITOR_AGE_S``, default 24h):

  DB rows (DELETE, fail-open, via the existing db.py WAL helpers):
    sessions / activity / breaker / breaker_events / posttool_frontier_counters
      keyed by skey -> reaped where time-col < cutoff AND skey NOT protected.
    specs / posttool_claims
      keyed by spec_key '<dirhash>/<safe>' -> reaped where updated_at < cutoff
      AND substr(spec_key,1,16) NOT protected.

  Legacy on-disk JSON (superseded by the DB; read once on a DB miss):
    ledgers/<skey>.json, breaker/<skey>.json
      mtime > age AND skey NOT protected -> unlink.
    specs/<dirhash>/<safe>/spec.json
      mtime > age AND dir_hash NOT protected -> unlink (+ remove empty buckets).

  Lock files (0-byte, recreated on demand; race-free via flock-probe):
    ledgers/*.pretool.lock, breaker/*.judge.lock, specs/locks/*.lock,
    judged/*.lock, searchd/*.lock
      mtime > age AND fcntl.flock(LOCK_EX|LOCK_NB) acquirable (no current
      holder) -> unlink. A held lock is NEVER unlinked.

  Daemon sockets (self-healing on next spawn; reaped to reduce clutter):
    judged/*.sock, searchd/*.sock
      mtime > age AND socket NOT connectable -> unlink.

  Provenance (durable history, separate retention):
    unifusion-runs/*.md
      mtime > UNIFABLE_JANITOR_PROVENANCE_AGE_S (default 30d) -> unlink.

  Dead alive-markers:
    alive/<skey>.json whose host is NOT currently live AND started_at > age
      -> unlink (the marker itself).

NEVER touched: bin/, versions/, current, the unifable.db schema
(only row DELETEs), and any skey/dir_hash with a live alive-marker.

Stdlib only; host-agnostic. Run: ``python3 scripts/gate/janitor.py --run``.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def _enabled() -> bool:
    return (os.environ.get("UNIFABLE_JANITOR", "1") or "1").strip().lower() not in ("0", "false", "no", "off")


def _data_root() -> Path:
    try:
        from ledger import data_root

        return data_root()
    except Exception:
        base = os.environ.get("UNIFABLE_DATA")
        return Path(base).expanduser() if base else Path.home() / ".unifable"


def _alive_dir() -> Path:
    return _data_root() / "alive"


def _sentinel_path() -> Path:
    return _alive_dir() / ".last_sweep"


def _dir_hash_from_root(root_str: str) -> str:
    """Mirror spec_io.dir_hash without re-running the git/walk-up resolution.

    The alive-marker stores the already-canonical project root (resolved by
    spec_io.canonical_project_root at write time), so hashing that string
    reproduces dir_hash(cwd) exactly."""
    return hashlib.sha256(str(root_str).encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Alive registry + liveness probe
# ---------------------------------------------------------------------------


def load_alive_registry() -> dict[str, dict[str, Any]]:
    """Read every ``alive/*.json`` marker. Fail-open -> {}."""
    out: dict[str, dict[str, Any]] = {}
    d = _alive_dir()
    try:
        if not d.is_dir():
            return out
        for entry in d.iterdir():
            if not entry.name.endswith(".json") or not entry.is_file():
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            skey = data.get("skey") or entry.name[:-5]
            out[str(skey)] = data
    except OSError:
        pass
    return out


def _ps_comm(pid: int, ps_provider: Callable[[int], tuple[int, str, str] | None] | None) -> str | None:
    if ps_provider is None:
        try:
            from process_host import _default_ps_line

            line = _default_ps_line(pid)
        except Exception:
            return None
    else:
        line = ps_provider(pid)
    if line is None:
        return None
    return line[1]


def is_marker_live(
    marker: dict[str, Any],
    *,
    ps_provider: Callable[[int], tuple[int, str, str] | None] | None = None,
) -> bool:
    """True when the marker's host process is alive (conservative on error).

    Probe: ``os.kill(pid, 0)`` then a comm match against the recorded host_comm.
    - ProcessLookupError -> dead -> False
    - PermissionError -> process exists (different user) -> True (alive)
    - comm match -> True; comm mismatch -> PID reuse -> False
    - any probe error -> True (cannot confirm dead -> do NOT reap)
    """
    pid = marker.get("host_pid")
    try:
        pid = int(pid) if pid is not None else 0
    except (TypeError, ValueError):
        pid = 0
    if pid <= 1:
        return False  # no liveness claim recorded
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not signalable by us -> treat as alive
    except OSError:
        return True  # unknown -> conservative: do not reap
    recorded_comm = str(marker.get("host_comm") or "").lower()
    if not recorded_comm:
        return True  # recorded without comm -> trust the live PID
    comm = _ps_comm(pid, ps_provider)
    if comm is None:
        return True  # ps failed -> conservative: do not reap
    # Substring match either direction defends against truncation/prefixing.
    return recorded_comm in comm.lower() or comm.lower() in recorded_comm


def _protected(
    registry: dict[str, dict[str, Any]],
    ps_provider: Callable[[int], tuple[int, str, str] | None] | None = None,
) -> tuple[set[str], set[str]]:
    """Return (protected_skeys, protected_dir_hashes) from live markers."""
    skeys: set[str] = set()
    dir_hashes: set[str] = set()
    for skey, marker in registry.items():
        if not is_marker_live(marker, ps_provider=ps_provider):
            continue
        skeys.add(skey)
        root = marker.get("project_root")
        if root:
            with contextlib.suppress(Exception):
                dir_hashes.add(_dir_hash_from_root(str(root)))
    return skeys, dir_hashes


# ---------------------------------------------------------------------------
# Reap budget
# ---------------------------------------------------------------------------


class _Budget:
    def __init__(self, total: int) -> None:
        self.remaining = total

    def take(self, n: int) -> int:
        if self.remaining <= 0:
            return 0
        n = max(0, n)
        granted = min(n, self.remaining)
        self.remaining -= granted
        return granted


# ---------------------------------------------------------------------------
# DB row reaping
# ---------------------------------------------------------------------------


def _utc_cutoff(age_s: int) -> str:
    from datetime import UTC, datetime

    return (datetime.now(UTC).replace(microsecond=0) - _timedelta(age_s)).isoformat()


def _timedelta(secs: int):
    from datetime import timedelta

    return timedelta(seconds=secs)


def _reap_db_rows(
    protected_skeys: set[str],
    protected_dir_hashes: set[str],
    age_s: int,
    budget: _Budget,
) -> int:
    """DELETE stale, non-protected rows across the keyed tables. Fail-open."""
    reaped = 0
    try:
        import db
    except Exception:
        return 0

    cutoff = _utc_cutoff(age_s)

    def _placeholders(n: int) -> str:
        return ",".join(["?"] * max(0, n))

    # skey-keyed tables -> (table, time_col). specs/posttool_claims are dir_hash-keyed.
    skey_tables = (
        ("sessions", "updated_at"),
        ("activity", "ts"),
        ("breaker", "updated_at"),
        ("breaker_events", "ts"),
        ("posttool_frontier_counters", "updated_at"),
    )
    dirhash_tables = (
        ("specs", "updated_at"),
        ("posttool_claims", "updated_at"),
    )

    def _delete_skey_table(conn, table: str, time_col: str) -> int:
        n = 0
        remaining = budget.remaining
        if remaining <= 0:
            return 0
        # Select stale, non-protected skeys (LIMIT bounded), then delete them by skey.
        prot = _placeholders(len(protected_skeys))
        where_prot = f"AND skey NOT IN ({prot})" if protected_skeys else ""
        rows = conn.execute(
            f"SELECT DISTINCT skey FROM {table} WHERE {time_col} != '' AND {time_col} < ? {where_prot} LIMIT {int(remaining)}",
            (cutoff, *protected_skeys),
        ).fetchall()
        if not rows:
            return 0
        keys = [r["skey"] if hasattr(r, "keys") else r[0] for r in rows]
        ph = _placeholders(len(keys))
        cur = conn.execute(f"DELETE FROM {table} WHERE skey IN ({ph})", keys)
        n = int(cur.rowcount or 0)
        budget.remaining -= n
        return n

    def _delete_dirhash_table(conn, table: str, time_col: str) -> int:
        n = 0
        remaining = budget.remaining
        if remaining <= 0:
            return 0
        prot = _placeholders(len(protected_dir_hashes))
        where_prot = f"AND substr(spec_key,1,16) NOT IN ({prot})" if protected_dir_hashes else ""
        rows = conn.execute(
            f"SELECT spec_key FROM {table} WHERE {time_col} != '' AND {time_col} < ? {where_prot} LIMIT {int(remaining)}",
            (cutoff, *protected_dir_hashes),
        ).fetchall()
        if not rows:
            return 0
        keys = [r["spec_key"] if hasattr(r, "keys") else r[0] for r in rows]
        ph = _placeholders(len(keys))
        cur = conn.execute(f"DELETE FROM {table} WHERE spec_key IN ({ph})", keys)
        n = int(cur.rowcount or 0)
        budget.remaining -= n
        return n

    try:
        with db.connect() as conn:
            if conn is None:
                return 0
            for table, tcol in skey_tables:
                if budget.remaining <= 0:
                    break
                with contextlib.suppress(Exception):
                    with db._immediate(conn):
                        reaped += _delete_skey_table(conn, table, tcol)
            for table, tcol in dirhash_tables:
                if budget.remaining <= 0:
                    break
                with contextlib.suppress(Exception):
                    with db._immediate(conn):
                        reaped += _delete_dirhash_table(conn, table, tcol)
    except Exception:
        pass
    return reaped


# ---------------------------------------------------------------------------
# Filesystem reaping
# ---------------------------------------------------------------------------


def _older_than(path: Path, age_s: int) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) > age_s
    except OSError:
        return False


def _unlink(path: Path, budget: _Budget) -> bool:
    if budget.remaining <= 0:
        return False
    try:
        os.unlink(path)
        budget.remaining -= 1
        return True
    except OSError:
        return False


def _reap_legacy_json(
    subdir: str,
    protected_skeys: set[str],
    age_s: int,
    budget: _Budget,
) -> int:
    """Reap ``<subdir>/<skey>.json`` legacy state. Skip ``*.lock``/``*.tmp``."""
    reaped = 0
    d = _data_root() / subdir
    try:
        if not d.is_dir():
            return 0
        for entry in d.iterdir():
            if budget.remaining <= 0:
                break
            name = entry.name
            if not name.endswith(".json"):
                continue  # leave .lock / .tmp / anything else
            stem = name[:-5]
            if stem in protected_skeys:
                continue
            if not _older_than(entry, age_s):
                continue
            if _unlink(entry, budget):
                reaped += 1
    except OSError:
        pass
    return reaped


def _reap_specs_legacy(protected_dir_hashes: set[str], age_s: int, budget: _Budget) -> int:
    """Reap ``specs/<dirhash>/<safe>/spec.json`` for non-protected, stale buckets."""
    reaped = 0
    root = _data_root() / "specs"
    try:
        if not root.is_dir():
            return 0
        for bucket in list(root.iterdir()):
            if budget.remaining <= 0:
                break
            if not bucket.is_dir() or bucket.name == "locks":
                continue
            if bucket.name in protected_dir_hashes:
                continue
            for sess in list(bucket.iterdir()):
                if budget.remaining <= 0:
                    break
                if not sess.is_dir():
                    continue
                spec = sess / "spec.json"
                if spec.is_file() and _older_than(spec, age_s):
                    if _unlink(spec, budget):
                        reaped += 1
                # remove empty session dir, then empty bucket dir
                with contextlib.suppress(OSError):
                    if not any(sess.iterdir()):
                        sess.rmdir()
            with contextlib.suppress(OSError):
                if not any(bucket.iterdir()):
                    bucket.rmdir()
    except OSError:
        pass
    return reaped


def _flock_acquirable(path: Path) -> bool:
    """True when no process currently holds an exclusive flock on *path*.

    Race-free lock reap: if we can take LOCK_EX|LOCK_NB, nobody holds it, so
    unlinking is safe. A held lock (live daemon / in-flight writer) is never
    unlinked. Releases the lock and closes the fd before unlinking.
    """
    fd = None
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(fd)
        return False  # held -> skip
    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)
    return True


def _reap_locks(age_s: int, budget: _Budget) -> int:
    """Reap stale ``*.lock`` files that no process currently holds."""
    reaped = 0
    subdirs = ("ledgers", "breaker", "specs/locks", "judged", "searchd")
    root = _data_root()
    for sub in subdirs:
        if budget.remaining <= 0:
            break
        d = root / sub
        try:
            if not d.is_dir():
                continue
            for entry in d.iterdir():
                if budget.remaining <= 0:
                    break
                if not entry.name.endswith(".lock") or not entry.is_file():
                    continue
                if not _older_than(entry, age_s):
                    continue
                if not _flock_acquirable(entry):
                    continue
                if _unlink(entry, budget):
                    reaped += 1
        except OSError:
            continue
    return reaped


def _socket_connectable(path: Path, timeout: float) -> bool:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(str(path))
        finally:
            with contextlib.suppress(OSError):
                s.close()
        return True
    except OSError:
        return False


def _reap_sockets(age_s: int, budget: _Budget) -> int:
    """Reap stale, non-connectable daemon ``*.sock`` files."""
    reaped = 0
    timeout = _env_float("UNIFABLE_JANITOR_SOCKET_TIMEOUT", 0.2)
    root = _data_root()
    for sub in ("judged", "searchd"):
        if budget.remaining <= 0:
            break
        d = root / sub
        try:
            if not d.is_dir():
                continue
            for entry in d.iterdir():
                if budget.remaining <= 0:
                    break
                if not entry.name.endswith(".sock") or not entry.is_file():
                    continue
                if not _older_than(entry, age_s):
                    continue
                if _socket_connectable(entry, timeout):
                    continue  # live daemon -> skip
                if _unlink(entry, budget):
                    reaped += 1
        except OSError:
            continue
    return reaped


def _reap_provenance(age_s: int, budget: _Budget) -> int:
    """Reap ``unifusion-runs/*.md`` older than the provenance retention window."""
    reaped = 0
    d = _data_root() / "unifusion-runs"
    try:
        if not d.is_dir():
            return 0
        for entry in d.iterdir():
            if budget.remaining <= 0:
                break
            if not entry.name.endswith(".md") or not entry.is_file():
                continue
            if not _older_than(entry, age_s):
                continue
            if _unlink(entry, budget):
                reaped += 1
    except OSError:
        pass
    return reaped


def _reap_dead_markers(
    registry: dict[str, dict[str, Any]],
    protected_skeys: set[str],
    age_s: int,
    budget: _Budget,
) -> int:
    """Reap alive-markers for sessions that are not currently live and are old."""
    reaped = 0
    d = _alive_dir()
    try:
        if not d.is_dir():
            return 0
        for entry in d.iterdir():
            if budget.remaining <= 0:
                break
            if not entry.name.endswith(".json") or not entry.is_file():
                continue
            stem = entry.name[:-5]
            if stem in protected_skeys:
                continue  # live right now -> keep its marker
            marker = registry.get(stem)
            # Age by mtime (started_at is ISO; mtime is the cheap, always-present signal).
            if not _older_than(entry, age_s):
                continue
            # Belt-and-suspenders: if the marker somehow still looks live, skip.
            if marker is not None and is_marker_live(marker):
                continue
            if _unlink(entry, budget):
                reaped += 1
    except OSError:
        pass
    return reaped


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    *,
    age_s: int | None = None,
    provenance_age_s: int | None = None,
    max_reap: int | None = None,
    ps_provider: Callable[[int], tuple[int, str, str] | None] | None = None,
) -> dict[str, int]:
    """Run one sweep. Never raises. Returns a per-target reap-count dict."""
    age_s = age_s if age_s is not None else _env_int("UNIFABLE_JANITOR_AGE_S", 86400)
    provenance_age_s = (
        provenance_age_s if provenance_age_s is not None else _env_int("UNIFABLE_JANITOR_PROVENANCE_AGE_S", 2592000)
    )
    max_reap = max_reap if max_reap is not None else _env_int("UNIFABLE_JANITOR_MAX_REAP", 50000)
    if age_s <= 0 or max_reap <= 0:
        return {}

    registry = load_alive_registry()
    protected_skeys, protected_dir_hashes = _protected(registry, ps_provider)
    budget = _Budget(max_reap)

    counts = {"db": 0, "ledgers": 0, "breaker": 0, "specs": 0, "locks": 0, "sockets": 0, "provenance": 0, "markers": 0}
    with contextlib.suppress(Exception):
        counts["db"] = _reap_db_rows(protected_skeys, protected_dir_hashes, age_s, budget)
    with contextlib.suppress(Exception):
        counts["ledgers"] = _reap_legacy_json("ledgers", protected_skeys, age_s, budget)
    with contextlib.suppress(Exception):
        counts["breaker"] = _reap_legacy_json("breaker", protected_skeys, age_s, budget)
    with contextlib.suppress(Exception):
        counts["specs"] = _reap_specs_legacy(protected_dir_hashes, age_s, budget)
    with contextlib.suppress(Exception):
        counts["locks"] = _reap_locks(age_s, budget)
    with contextlib.suppress(Exception):
        counts["sockets"] = _reap_sockets(age_s, budget)
    with contextlib.suppress(Exception):
        counts["provenance"] = _reap_provenance(provenance_age_s, budget)
    with contextlib.suppress(Exception):
        counts["markers"] = _reap_dead_markers(registry, protected_skeys, age_s, budget)

    # Touch the sweep sentinel (mtime = last sweep time).
    with contextlib.suppress(Exception):
        _alive_dir().mkdir(parents=True, exist_ok=True)
        Path(_sentinel_path()).touch()

    return counts


def main(argv: list[str]) -> int:
    if "--run" not in argv:
        return 0
    if not _enabled():
        return 0
    try:
        run()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    import sys as _sys

    raise SystemExit(main(_sys.argv))
