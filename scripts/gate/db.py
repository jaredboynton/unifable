#!/usr/bin/env python3
"""Single SQLite backend for unifable gate state. Stdlib only; host-agnostic.

This module consolidates the gate's previously-separate JSON stores -- the
per-session ledger, the groundedness breaker, the evidence spec, and the
per-project findings -- into one WAL-mode SQLite database at
``<data_root>/unifable.db`` (data_root honors $UNIFABLE_DATA).

Why SQLite: the JSON stores existed only to simulate transactions on flat files
(unique-temp + os.replace, last-writer-wins, and two POSIX flocks). WAL gives
that for free -- concurrent readers never block, a single writer serializes for
microseconds, and read-modify-write becomes a real transaction instead of a
lossy load-modify-save.

FAIL-OPEN CONTRACT (sacred, per AGENTS.md): every public accessor degrades to
empty/unblocked on ANY error and never raises into a hook. A gate that
hard-locks a session on its own DB bug is worse than no gate. The expensive
judge call is coalesced OUTSIDE this module (the breaker flock) and is NEVER
held inside a transaction -- that would pin the single WAL writer slot and wedge
every session.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# data_root lives in ledger.py (the historical home of the shared root). ledger
# imports db only lazily (inside its accessors), so this top-level import is
# cycle-free.
try:  # bare import on hooks/tests sys.path; package import otherwise
    from ledger import data_root
except ImportError:  # pragma: no cover
    from scripts.gate.ledger import data_root

SCHEMA_VERSION = 4
APPLICATION_ID = 0x554E4642  # "UNFB"
DEFAULT_BUSY_MS = 5000

# Activity kinds moved out of the ledger soup into their own deduplicated table.
# These mirror the legacy ledger list field names <-> kinds.
ACTIVITY_LIST_TO_KIND = {
    "read_paths": "read_path",
    "fetched_urls": "fetched_url",
    "ran_commands": "ran_command",
    "tool_evidence": "tool_evidence",
    "command_outputs": "command_output",
}
ACTIVITY_KIND_TO_LIST = {v: k for k, v in ACTIVITY_LIST_TO_KIND.items()}

_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    skey         TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL DEFAULT '',
    project_root TEXT NOT NULL DEFAULT '',
    data         TEXT NOT NULL DEFAULT '{}',
    updated_at   TEXT NOT NULL DEFAULT '',
    expires_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS activity (
    skey    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    value   TEXT NOT NULL,
    ts      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (skey, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_activity_skey_kind ON activity(skey, kind, ts);

CREATE TABLE IF NOT EXISTS breaker (
    skey       TEXT PRIMARY KEY,
    data       TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS breaker_events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    skey   TEXT NOT NULL,
    kind   TEXT NOT NULL,
    ts     TEXT NOT NULL DEFAULT '',
    fields TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_breaker_events_skey ON breaker_events(skey, id);

CREATE TABLE IF NOT EXISTS specs (
    spec_key   TEXT PRIMARY KEY,
    doc        TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS posttool_claims (
    spec_key   TEXT PRIMARY KEY,
    call_at    REAL NOT NULL DEFAULT 0,
    epoch      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS posttool_frontier_counters (
    skey             TEXT PRIMARY KEY,
    research_tools   INTEGER NOT NULL DEFAULT 0,
    discovery_count  INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL DEFAULT ''
);

-- Background reconcile/discover (fire-and-forget) state, keyed by spec_key.
-- `lease_at` is the in-flight claim timestamp (spawn debounce); `pending` holds
-- the completed context the child enqueues for the next PreToolUse to drain.
CREATE TABLE IF NOT EXISTS posttool_bg (
    spec_key   TEXT PRIMARY KEY,
    lease_at   REAL NOT NULL DEFAULT 0,
    pending    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS breaker_release_bg (
    rel_key    TEXT PRIMARY KEY,
    lease_at   REAL NOT NULL DEFAULT 0,
    pending    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS projects (
    root_hash        TEXT PRIMARY KEY,
    root             TEXT NOT NULL DEFAULT '',
    findings_counter INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    root_hash       TEXT NOT NULL,
    local_num       INTEGER NOT NULL,
    fid             TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    severity        TEXT NOT NULL DEFAULT 'low',
    source          TEXT NOT NULL DEFAULT '',
    location        TEXT NOT NULL DEFAULT '',
    evidence        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    resolution      TEXT NOT NULL DEFAULT '',
    verify_cmd      TEXT NOT NULL DEFAULT '',
    verify_evidence TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT '',
    UNIQUE (root_hash, fid)
);
CREATE INDEX IF NOT EXISTS idx_findings_blocking ON findings(root_hash, severity, status);
"""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def db_path() -> Path:
    return data_root() / "unifable.db"


def _busy_ms() -> int:
    try:
        return max(100, min(20000, int(os.environ.get("UNIFABLE_DB_BUSY_TIMEOUT_MS", str(DEFAULT_BUSY_MS)))))
    except (TypeError, ValueError):
        return DEFAULT_BUSY_MS


def _json_loads(text: Any, default: Any) -> Any:
    if not text:
        return default
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return default
    return value


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    # journal_mode must be set outside a transaction; isolation_level=None
    # (autocommit) guarantees that. WAL is persistent across reopen, but
    # re-asserting is cheap and lets a freshly-created file converge.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_busy_ms()}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA wal_autocheckpoint=1000")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    have = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if have > SCHEMA_VERSION:
        # DB written by a newer plugin: do not migrate down. Reads still work for
        # the columns we know; we simply never bump it back.
        return
    if have == SCHEMA_VERSION:
        return
    # All DDL is IF NOT EXISTS + additive, so concurrent first-openers each run it
    # idempotently; busy_timeout absorbs the brief DDL lock. executescript manages
    # its own transaction -- do not wrap it in a manual BEGIN.
    conn.executescript(_SCHEMA_DDL)
    conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


@contextlib.contextmanager
def connect():
    """Yield a short-lived autocommit connection, or None on any failure.

    Callers MUST treat a yielded None as "DB unavailable -> fail open." A corrupt
    or unreadable file is moved aside once and recreated; the window degrades to
    empty/unblocked, never wedged.
    """
    conn: sqlite3.Connection | None = None
    try:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=_busy_ms() / 1000.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        _ensure_schema(conn)
    except sqlite3.DatabaseError:
        with contextlib.suppress(Exception):
            if conn is not None:
                conn.close()
            conn = None
            corrupt = db_path().with_suffix(".db.corrupt")
            with contextlib.suppress(OSError):
                db_path().replace(corrupt)
        try:
            conn = sqlite3.connect(str(db_path()), timeout=_busy_ms() / 1000.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            _apply_pragmas(conn)
            _ensure_schema(conn)
        except Exception:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            yield None
            return
    except Exception:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        yield None
        return
    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.close()


@contextlib.contextmanager
def _immediate(conn: sqlite3.Connection):
    """A short write transaction. BEGIN IMMEDIATE declares write intent up front,
    avoiding the read->write upgrade that returns SQLITE_BUSY bypassing
    busy_timeout. NEVER do slow work (judge/network) inside this block: it holds
    the single global writer slot."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _read(fn: Callable[[sqlite3.Connection], Any], default: Any) -> Any:
    try:
        with connect() as conn:
            if conn is None:
                return default
            return fn(conn)
    except Exception:
        return default


def _write(fn: Callable[[sqlite3.Connection], Any], default: Any) -> Any:
    try:
        with connect() as conn:
            if conn is None:
                return default
            with _immediate(conn):
                return fn(conn)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Sessions (ledger scalar soup) + activity
# ---------------------------------------------------------------------------


def session_load(skey: str) -> dict[str, Any] | None:
    """Return the merged ledger dict for *skey* (scalar JSON + activity lists),
    or None when neither a session row nor any activity exists for it."""

    def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute("SELECT data FROM sessions WHERE skey=?", (skey,)).fetchone()
        acts = _activity_lists(conn, skey)
        has_acts = any(acts.values())
        if row is None and not has_acts:
            return None
        data: dict[str, Any] = _json_loads(row["data"], {}) if row is not None else {}
        if not isinstance(data, dict):
            data = {}
        for list_name, values in acts.items():
            data[list_name] = values
        return data

    return _read(op, None)


def session_save(skey: str, ledger: dict[str, Any], *, session_id: str = "", project_root: str = "") -> None:
    """Persist *ledger*: activity lists -> activity table (dedup), the rest ->
    sessions.data JSON."""

    def op(conn: sqlite3.Connection) -> None:
        scalar = {k: v for k, v in ledger.items() if k not in ACTIVITY_LIST_TO_KIND}
        now = utc_now()
        conn.execute(
            "INSERT INTO sessions(skey, session_id, project_root, data, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(skey) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at, "
            "session_id=CASE WHEN excluded.session_id != '' THEN excluded.session_id ELSE sessions.session_id END, "
            "project_root=CASE WHEN excluded.project_root != '' THEN excluded.project_root ELSE sessions.project_root END",
            (skey, session_id or "", project_root or "", _json_dumps(scalar), now),
        )
        for list_name, kind in ACTIVITY_LIST_TO_KIND.items():
            for value in ledger.get(list_name) or []:
                if not value:
                    continue
                conn.execute(
                    "INSERT INTO activity(skey, kind, value, ts) VALUES(?,?,?,?) ON CONFLICT(skey, kind, value) DO NOTHING",
                    (skey, kind, str(value), now),
                )

    _write(op, None)


def _activity_lists(conn: sqlite3.Connection, skey: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {name: [] for name in ACTIVITY_LIST_TO_KIND}
    rows = conn.execute(
        "SELECT kind, value FROM activity WHERE skey=? ORDER BY ts, value",
        (skey,),
    ).fetchall()
    for row in rows:
        list_name = ACTIVITY_KIND_TO_LIST.get(row["kind"])
        if list_name:
            out[list_name].append(row["value"])
    return out


def activity_add(skey: str, kind: str, values: list[str]) -> None:
    """Append activity values (idempotent). *kind* is a ledger list name."""
    db_kind = ACTIVITY_LIST_TO_KIND.get(kind, kind)

    def op(conn: sqlite3.Connection) -> None:
        now = utc_now()
        for value in values:
            if not value:
                continue
            conn.execute(
                "INSERT INTO activity(skey, kind, value, ts) VALUES(?,?,?,?) ON CONFLICT(skey, kind, value) DO NOTHING",
                (skey, db_kind, str(value), now),
            )

    _write(op, None)


# ---------------------------------------------------------------------------
# Breaker state + events
# ---------------------------------------------------------------------------


def breaker_load(skey: str) -> dict[str, Any] | None:
    """Return the breaker dict (scalars + 'events' list) or None when absent."""

    def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute("SELECT data FROM breaker WHERE skey=?", (skey,)).fetchone()
        ev_rows = conn.execute(
            "SELECT kind, ts, fields FROM breaker_events WHERE skey=? ORDER BY id",
            (skey,),
        ).fetchall()
        if row is None and not ev_rows:
            return None
        data: dict[str, Any] = _json_loads(row["data"], {}) if row is not None else {}
        if not isinstance(data, dict):
            data = {}
        events = []
        for er in ev_rows:
            fields = _json_loads(er["fields"], {})
            if not isinstance(fields, dict):
                fields = {}
            events.append({"kind": er["kind"], "ts": er["ts"], **fields})
        data["events"] = events
        return data

    return _read(op, None)


def breaker_save(skey: str, state: dict[str, Any]) -> None:
    """Persist breaker scalars (sans events) + rewrite the event log rows."""

    def op(conn: sqlite3.Connection) -> None:
        scalar = {k: v for k, v in state.items() if k != "events"}
        now = utc_now()
        conn.execute(
            "INSERT INTO breaker(skey, data, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(skey) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (skey, _json_dumps(scalar), now),
        )
        conn.execute("DELETE FROM breaker_events WHERE skey=?", (skey,))
        for event in state.get("events") or []:
            if not isinstance(event, dict):
                continue
            kind = str(event.get("kind") or "")
            ts = str(event.get("ts") or "")
            fields = {k: v for k, v in event.items() if k not in ("kind", "ts")}
            conn.execute(
                "INSERT INTO breaker_events(skey, kind, ts, fields) VALUES(?,?,?,?)",
                (skey, kind, ts, _json_dumps(fields)),
            )

    _write(op, None)


# ---------------------------------------------------------------------------
# Specs (the durable evidence/task board, stored as one JSON doc per key)
# ---------------------------------------------------------------------------


def spec_load(spec_key: str) -> dict[str, Any] | None:
    def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute("SELECT doc FROM specs WHERE spec_key=?", (spec_key,)).fetchone()
        if row is None:
            return None
        doc = _json_loads(row["doc"], None)
        return doc if isinstance(doc, dict) else None

    return _read(op, None)


def spec_save(spec_key: str, doc: dict[str, Any]) -> None:
    def op(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO specs(spec_key, doc, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(spec_key) DO UPDATE SET doc=excluded.doc, updated_at=excluded.updated_at",
            (spec_key, _json_dumps(doc), utc_now()),
        )

    _write(op, None)


def spec_keys() -> list[str]:
    """All spec keys present (for the fragmented-spec relocation scan)."""
    return _read(lambda c: [r["spec_key"] for r in c.execute("SELECT spec_key FROM specs")], [])


def spec_delete(spec_key: str) -> None:
    _write(lambda c: c.execute("DELETE FROM specs WHERE spec_key=?", (spec_key,)), None)


# ---------------------------------------------------------------------------
# PostToolUse session-level judging coalesce (atomic compare-and-set)
# ---------------------------------------------------------------------------


def posttool_spec_claim(spec_key: str, ts: float, epoch: str, window: float) -> bool:
    """Atomic coalesce claim for session-level PostToolUse spec judging.

    Returns True if this caller should run reconcile+discover for *spec_key*, False
    if a sibling already claimed within *window* seconds of the same *epoch* (turn).
    The read-decide-write runs in ONE BEGIN IMMEDIATE transaction against a DEDICATED
    table, so -- unlike a ledger-blob field that a concurrent whole-row writer can
    clobber -- "exactly one claimant per window" holds across a parallel tool batch.

    When *epoch* is empty the turn fingerprint is unreliable; coalescing MUST NOT
    suppress work, so this returns True immediately (fail-open to run). Fail-open on
    any other DB error also returns True."""
    if not epoch:
        return True

    def op(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT call_at, epoch FROM posttool_claims WHERE spec_key=?", (spec_key,)).fetchone()
        if row is not None:
            last_epoch = row["epoch"] or ""
            try:
                recent = epoch == last_epoch and abs(float(ts) - float(row["call_at"])) < float(window)
            except (TypeError, ValueError):
                recent = False
            if recent:
                return False
        conn.execute(
            "INSERT INTO posttool_claims(spec_key, call_at, epoch, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(spec_key) DO UPDATE SET call_at=excluded.call_at, epoch=excluded.epoch, "
            "updated_at=excluded.updated_at",
            (spec_key, float(ts), epoch, utc_now()),
        )
        return True

    return _write(op, True)


# ---------------------------------------------------------------------------
# PostToolUse HEAVY frontier counters (atomic increments)
# ---------------------------------------------------------------------------


def frontier_bump_research(skey: str) -> tuple[int, int]:
    """Atomically increment the HEAVY research-tool counter for *skey*.

    Returns ``(research_tools, discovery_count)`` after the bump. Fail-open: ``(0, 0)``."""

    def op(conn: sqlite3.Connection) -> tuple[int, int]:
        now = utc_now()
        conn.execute(
            "INSERT INTO posttool_frontier_counters(skey, research_tools, discovery_count, updated_at) "
            "VALUES(?, 1, 0, ?) "
            "ON CONFLICT(skey) DO UPDATE SET research_tools=research_tools+1, updated_at=excluded.updated_at",
            (skey, now),
        )
        row = conn.execute(
            "SELECT research_tools, discovery_count FROM posttool_frontier_counters WHERE skey=?",
            (skey,),
        ).fetchone()
        return int(row["research_tools"]), int(row["discovery_count"])

    return _write(op, (0, 0))


def frontier_bump_discovery(skey: str) -> int:
    """Atomically increment the frontier-discovery counter for *skey*.

    Returns the new discovery count. Fail-open: ``0``."""

    def op(conn: sqlite3.Connection) -> int:
        now = utc_now()
        conn.execute(
            "INSERT INTO posttool_frontier_counters(skey, research_tools, discovery_count, updated_at) "
            "VALUES(?, 0, 1, ?) "
            "ON CONFLICT(skey) DO UPDATE SET discovery_count=discovery_count+1, updated_at=excluded.updated_at",
            (skey, now),
        )
        row = conn.execute(
            "SELECT discovery_count FROM posttool_frontier_counters WHERE skey=?",
            (skey,),
        ).fetchone()
        return int(row["discovery_count"])

    return _write(op, 0)


def frontier_get_counts(skey: str) -> tuple[int, int]:
    """Read HEAVY frontier counters without mutating. Fail-open: ``(0, 0)``."""

    def op(conn: sqlite3.Connection) -> tuple[int, int]:
        row = conn.execute(
            "SELECT research_tools, discovery_count FROM posttool_frontier_counters WHERE skey=?",
            (skey,),
        ).fetchone()
        if row is None:
            return 0, 0
        return int(row["research_tools"]), int(row["discovery_count"])

    return _read(op, (0, 0))


# ---------------------------------------------------------------------------
# Background reconcile/discover lease + pending-context queue (fire-and-forget)
# ---------------------------------------------------------------------------


def posttool_bg_lease(spec_key: str, ts: float, ttl: float) -> bool:
    """Atomic in-flight claim for the background reconcile/discover job.

    Returns True when THIS caller should spawn the detached job for *spec_key*,
    False when a job leased within *ttl* seconds is still considered in-flight (so
    sequential evidence-changing tools do not spawn a process storm). The
    read-decide-write runs in ONE BEGIN IMMEDIATE transaction on a dedicated table.
    Fail-open: any DB error returns True (spawn rather than starve reconcile)."""

    def op(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT lease_at FROM posttool_bg WHERE spec_key=?", (spec_key,)).fetchone()
        if row is not None:
            try:
                if abs(float(ts) - float(row["lease_at"])) < float(ttl):
                    return False
            except (TypeError, ValueError):
                pass
        conn.execute(
            "INSERT INTO posttool_bg(spec_key, lease_at, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(spec_key) DO UPDATE SET lease_at=excluded.lease_at, updated_at=excluded.updated_at",
            (spec_key, float(ts), utc_now()),
        )
        return True

    return _write(op, True)


def posttool_bg_push(spec_key: str, body: str) -> None:
    """Enqueue completed background reconcile context for *spec_key*.

    Appends to any not-yet-drained pending block (blank-line separated) and clears
    the in-flight lease so the next evidence-changing tool may spawn again. Fail-open."""
    text = str(body or "").strip()

    def op(conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT pending FROM posttool_bg WHERE spec_key=?", (spec_key,)).fetchone()
        prior = str(row["pending"]) if row is not None and row["pending"] else ""
        merged = (prior + "\n\n" + text).strip() if (prior and text) else (text or prior)
        conn.execute(
            "INSERT INTO posttool_bg(spec_key, lease_at, pending, updated_at) VALUES(?,0,?,?) "
            "ON CONFLICT(spec_key) DO UPDATE SET pending=excluded.pending, lease_at=0, updated_at=excluded.updated_at",
            (spec_key, merged, utc_now()),
        )

    _write(op, None)


def posttool_bg_drain(spec_key: str) -> str:
    """Atomically read-and-clear the pending background context for *spec_key*.

    Returns the queued context (possibly empty). The clear runs in the same write
    transaction so a concurrent PreToolUse cannot drain the same block twice.
    Fail-open: any DB error returns ""."""

    def op(conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT pending FROM posttool_bg WHERE spec_key=?", (spec_key,)).fetchone()
        if row is None or not row["pending"]:
            return ""
        body = str(row["pending"])
        conn.execute(
            "UPDATE posttool_bg SET pending='', updated_at=? WHERE spec_key=?",
            (utc_now(), spec_key),
        )
        return body

    return _write(op, "")




def breaker_release_lease(rel_key: str, ts: float, ttl: float) -> bool:
    """Atomic in-flight claim for the background breaker-release (disarm) job.

    Returns True when THIS caller should spawn the detached disarm worker for
    *rel_key* (breaker key + claim + repo/tool fingerprint), False when a job
    leased within *ttl* seconds is still in-flight (so a burst of release-tool
    calls does not spawn a process storm). One BEGIN IMMEDIATE transaction.
    Fail-open: any DB error returns True (spawn rather than starve the disarm)."""

    def op(conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT lease_at FROM breaker_release_bg WHERE rel_key=?", (rel_key,)).fetchone()
        if row is not None:
            try:
                if abs(float(ts) - float(row["lease_at"])) < float(ttl):
                    return False
            except (TypeError, ValueError):
                pass
        conn.execute(
            "INSERT INTO breaker_release_bg(rel_key, lease_at, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(rel_key) DO UPDATE SET lease_at=excluded.lease_at, updated_at=excluded.updated_at",
            (rel_key, float(ts), utc_now()),
        )
        return True

    return _write(op, True)


def breaker_release_push(rel_key: str, body: str) -> None:
    """Enqueue a completed background disarm message for *rel_key*.

    Appends to any not-yet-drained pending block (blank-line separated) and clears
    the in-flight lease so a later release tool may spawn again. Fail-open."""
    text = str(body or "").strip()

    def op(conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT pending FROM breaker_release_bg WHERE rel_key=?", (rel_key,)).fetchone()
        prior = str(row["pending"]) if row is not None and row["pending"] else ""
        merged = (prior + "\n\n" + text).strip() if (prior and text) else (text or prior)
        conn.execute(
            "INSERT INTO breaker_release_bg(rel_key, lease_at, pending, updated_at) VALUES(?,0,?,?) "
            "ON CONFLICT(rel_key) DO UPDATE SET pending=excluded.pending, lease_at=0, updated_at=excluded.updated_at",
            (rel_key, merged, utc_now()),
        )

    _write(op, None)


def breaker_release_drain(rel_key: str) -> str:
    """Atomically read-and-clear the pending disarm message for *rel_key*.

    The clear runs in the same write transaction so a concurrent PreToolUse cannot
    drain the same block twice. Fail-open: any DB error returns ""."""

    def op(conn: sqlite3.Connection) -> str:
        row = conn.execute("SELECT pending FROM breaker_release_bg WHERE rel_key=?", (rel_key,)).fetchone()
        if row is None or not row["pending"]:
            return ""
        body = str(row["pending"])
        conn.execute(
            "UPDATE breaker_release_bg SET pending='', updated_at=? WHERE rel_key=?",
            (utc_now(), rel_key),
        )
        return body

    return _write(op, "")
def findings_load(root_hash: str) -> dict[str, Any]:
    """Return {'findings': {fid: row}, 'counter': N} for a project."""

    def op(conn: sqlite3.Connection) -> dict[str, Any]:
        crow = conn.execute("SELECT findings_counter FROM projects WHERE root_hash=?", (root_hash,)).fetchone()
        counter = int(crow["findings_counter"]) if crow is not None else 0
        out: dict[str, Any] = {"findings": {}, "counter": counter}
        rows = conn.execute(
            "SELECT fid, title, severity, source, location, evidence, status, resolution, "
            "verify_cmd, verify_evidence, created_at FROM findings WHERE root_hash=? ORDER BY id",
            (root_hash,),
        ).fetchall()
        for r in rows:
            out["findings"][r["fid"]] = {
                "id": r["fid"],
                "title": r["title"],
                "severity": r["severity"],
                "source": r["source"],
                "location": r["location"],
                "evidence": r["evidence"],
                "status": r["status"],
                "resolution": r["resolution"],
                "verify_cmd": r["verify_cmd"],
                "verify_evidence": r["verify_evidence"],
                "created": r["created_at"],
            }
        return out

    return _read(op, {"findings": {}, "counter": 0})


def findings_replace(root_hash: str, root: str, data: dict[str, Any]) -> None:
    """Rewrite a project's entire findings set to match *data* (the save_findings
    shim). Rarely used -- add_finding/finding_set_status mutate rows directly."""

    def op(conn: sqlite3.Connection) -> None:
        counter = 0
        with contextlib.suppress(TypeError, ValueError):
            counter = int(data.get("counter", 0) or 0)
        conn.execute(
            "INSERT INTO projects(root_hash, root, findings_counter) VALUES(?,?,?) "
            "ON CONFLICT(root_hash) DO UPDATE SET root=excluded.root, findings_counter=excluded.findings_counter",
            (root_hash, root, counter),
        )
        conn.execute("DELETE FROM findings WHERE root_hash=?", (root_hash,))
        for fid, f in (data.get("findings") or {}).items():
            if not isinstance(f, dict):
                continue
            num = 0
            tail = str(fid).rsplit("-", 1)[-1]
            if tail.isdigit():
                num = int(tail)
            conn.execute(
                "INSERT INTO findings(root_hash, local_num, fid, title, severity, source, location, evidence, "
                "status, resolution, verify_cmd, verify_evidence, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    root_hash,
                    num,
                    str(fid),
                    str(f.get("title", "")),
                    str(f.get("severity", "low")),
                    str(f.get("source", "")),
                    str(f.get("location", "")),
                    str(f.get("evidence", "")),
                    str(f.get("status", "open")),
                    str(f.get("resolution", "")),
                    str(f.get("verify_cmd", "")),
                    str(f.get("verify_evidence", "")),
                    str(f.get("created", "")),
                ),
            )

    _write(op, None)


def finding_add(
    root_hash: str,
    root: str,
    slug: str,
    title: str,
    severity: str,
    *,
    source: str = "",
    location: str = "",
    evidence: str = "",
) -> str | None:
    """Mint a per-project finding id atomically (counter bump + insert in one
    BEGIN IMMEDIATE). Returns the fid 'slug-N', or None on failure (fail open)."""

    def op(conn: sqlite3.Connection) -> str:
        conn.execute(
            "INSERT INTO projects(root_hash, root, findings_counter) VALUES(?,?,0) "
            "ON CONFLICT(root_hash) DO UPDATE SET root=excluded.root",
            (root_hash, root),
        )
        conn.execute("UPDATE projects SET findings_counter=findings_counter+1 WHERE root_hash=?", (root_hash,))
        num = int(conn.execute("SELECT findings_counter FROM projects WHERE root_hash=?", (root_hash,)).fetchone()[0])
        fid = f"{slug}-{num}"
        conn.execute(
            "INSERT INTO findings(root_hash, local_num, fid, title, severity, source, location, evidence, "
            "status, created_at) VALUES(?,?,?,?,?,?,?,?, 'open', ?)",
            (root_hash, num, fid, title, severity, source, location, evidence, utc_now()),
        )
        return fid

    return _write(op, None)


def finding_set_status(
    root_hash: str,
    fid: str,
    status: str,
    *,
    resolution: str | None = None,
    verify_cmd: str | None = None,
    verify_evidence: str | None = None,
) -> dict[str, Any] | None:
    """Update a finding's status (+optional fields). Returns the updated row dict,
    None if the DB is unavailable, or raises KeyError if the fid does not exist."""

    def op(conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute("SELECT id FROM findings WHERE root_hash=? AND fid=?", (root_hash, fid)).fetchone()
        if row is None:
            raise KeyError(fid)
        sets = ["status=?"]
        params: list[Any] = [status]
        if resolution is not None:
            sets.append("resolution=?")
            params.append(resolution)
        if verify_cmd is not None:
            sets.append("verify_cmd=?")
            params.append(verify_cmd)
        if verify_evidence is not None:
            sets.append("verify_evidence=?")
            params.append(verify_evidence)
        params.extend([root_hash, fid])
        conn.execute(f"UPDATE findings SET {', '.join(sets)} WHERE root_hash=? AND fid=?", params)
        updated = conn.execute(
            "SELECT fid, title, severity, source, location, evidence, status, resolution, "
            "verify_cmd, verify_evidence, created_at FROM findings WHERE root_hash=? AND fid=?",
            (root_hash, fid),
        ).fetchone()
        return {
            "id": updated["fid"],
            "title": updated["title"],
            "severity": updated["severity"],
            "source": updated["source"],
            "location": updated["location"],
            "evidence": updated["evidence"],
            "status": updated["status"],
            "resolution": updated["resolution"],
            "verify_cmd": updated["verify_cmd"],
            "verify_evidence": updated["verify_evidence"],
            "created": updated["created_at"],
        }

    # KeyError must propagate (callers rely on it); only DB errors fail open.
    try:
        with connect() as conn:
            if conn is None:
                return None
            with _immediate(conn):
                return op(conn)
    except KeyError:
        raise
    except Exception:
        return None
