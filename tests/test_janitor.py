#!/usr/bin/env python3
"""Tests for the fire-and-forget ~/.unifable/ janitor (scripts/gate/janitor.py).

The sacred property under test: a session whose host process is still alive is
NEVER reaped, even when its on-disk state looks stale by mtime/age. Liveness is
the alive-registry + host-PID probe (os.kill + comm match), not file recency.

Run: python3 -m pytest tests/test_janitor.py -q
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "gate"))

import fcntl  # noqa: E402

import db  # noqa: E402
import janitor  # noqa: E402
import ledger  # noqa: E402
import process_host  # noqa: E402
from spec_io import dir_hash  # noqa: E402

AGE_S = 86400  # 24h
PROV_AGE_S = 2592000  # 30d


def _age(path: Path, seconds_ago: float) -> None:
    t = time.time() - seconds_ago
    os.utime(path, (t, t))


def _stale(path: Path) -> None:
    _age(path, AGE_S + 3600)  # 25h old -> past the 24h threshold


def _write_marker(root: Path, skey: str, *, host_pid: int, host_comm: str = "claude", project_root: str = "/repo") -> None:
    alive = root / "alive"
    alive.mkdir(parents=True, exist_ok=True)
    (alive / f"{skey}.json").write_text(
        json.dumps(
            {
                "skey": skey,
                "session_id": "sid",
                "project_root": project_root,
                "host_pid": host_pid,
                "host_comm": host_comm,
                "started_at": "2026-06-01T00:00:00+00:00",
            }
        )
    )


def _fake_ps(tree: dict[int, tuple[int, str, str]]):
    def provider(pid: int):
        return tree.get(pid)

    return provider


# ---------------------------------------------------------------------------
# process_host ancestry walk
# ---------------------------------------------------------------------------


class TestProcessHost:
    def test_walks_to_host(self):
        # 100 (python) -> 90 (sh) -> 80 (claude) -> 1
        tree = {
            100: (90, "python3", "python3 session_start.py"),
            90: (80, "sh", "sh -c unifable-hook"),
            80: (1, "claude", "claude"),
        }
        host = process_host.find_host_ancestor(start_pid=100, ps_provider=_fake_ps(tree))
        assert host == (80, "claude")

    def test_node_launched_claude_via_command_hint(self):
        tree = {
            100: (90, "node", "node /usr/local/bin/claude --tui"),
            90: (1, "sh", "sh -c ..."),
        }
        host = process_host.find_host_ancestor(start_pid=100, ps_provider=_fake_ps(tree))
        assert host == (100, "node")

    def test_no_host_returns_none(self):
        tree = {100: (90, "python3", "python3"), 90: (1, "sh", "sh")}
        assert process_host.find_host_ancestor(start_pid=100, ps_provider=_fake_ps(tree)) is None

    def test_cycle_safe_returns_none(self):
        # Defensive: a malformed tree with a cycle must not loop forever.
        tree = {100: (90, "python3", "python3"), 90: (100, "sh", "sh")}
        assert process_host.find_host_ancestor(start_pid=100, ps_provider=_fake_ps(tree)) is None


# ---------------------------------------------------------------------------
# Liveness probe
# ---------------------------------------------------------------------------


class TestLiveness:
    def test_live_pid_matching_comm_is_live(self, monkeypatch):
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)  # no exception -> exists
        ps = _fake_ps({12345: (1, "claude", "claude")})
        assert janitor.is_marker_live({"host_pid": 12345, "host_comm": "claude"}, ps_provider=ps) is True

    def test_dead_pid_not_live(self, monkeypatch):
        def raise_no_pid(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(os, "kill", raise_no_pid)
        assert janitor.is_marker_live({"host_pid": 12345, "host_comm": "claude"}) is False

    def test_pid_reuse_comm_mismatch_not_live(self, monkeypatch):
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)  # exists
        ps = _fake_ps({12345: (1, "firefox", "firefox")})  # different comm -> PID reused
        assert janitor.is_marker_live({"host_pid": 12345, "host_comm": "claude"}, ps_provider=ps) is False

    def test_permission_error_treated_as_alive(self, monkeypatch):
        def deny(pid, sig):
            raise PermissionError

        monkeypatch.setattr(os, "kill", deny)
        assert janitor.is_marker_live({"host_pid": 12345, "host_comm": "claude"}) is True

    def test_no_host_pid_not_live(self):
        assert janitor.is_marker_live({"host_pid": 0, "host_comm": ""}) is False
        assert janitor.is_marker_live({}) is False


# ---------------------------------------------------------------------------
# Filesystem reap (legacy JSON, locks, sockets, provenance, never-touch)
# ---------------------------------------------------------------------------


class TestFilesystemReap:
    def test_stale_legacy_json_reaped_without_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
        led = tmp_path / "ledgers"
        led.mkdir()
        stale = led / "aaaa.json"
        stale.write_text("{}")
        _stale(stale)
        fresh = led / "bbbb.json"
        fresh.write_text("{}")
        counts = janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=lambda pid: None)
        assert not stale.exists()
        assert fresh.exists()
        assert counts["ledgers"] == 1

    def test_live_marker_skey_not_reaped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        ps = _fake_ps({12345: (1, "claude", "claude")})
        led = tmp_path / "ledgers"
        led.mkdir()
        skey = "protected-skey"
        stale = led / f"{skey}.json"
        stale.write_text("{}")
        _stale(stale)
        _write_marker(tmp_path, skey, host_pid=12345, project_root="/repo")
        counts = janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=ps)
        assert stale.exists(), "live session state must never be reaped"
        assert counts["ledgers"] == 0

    def test_held_lock_never_unlinked(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
        led = tmp_path / "ledgers"
        led.mkdir()
        lock = led / "held.json.pretool.lock"
        lock.write_text("")
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _stale(lock)
            janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=lambda pid: None)
            assert lock.exists(), "a lock held by a live writer/daemon must never be reaped"
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_unheld_stale_lock_reaped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
        led = tmp_path / "ledgers"
        led.mkdir()
        lock = led / "free.json.pretool.lock"
        lock.write_text("")
        _stale(lock)
        janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=lambda pid: None)
        assert not lock.exists()

    def test_dead_socket_reaped_live_socket_kept(self, tmp_path, monkeypatch):
        import shutil
        import tempfile

        # AF_UNIX path limit is 104 chars; pytest's tmp_path is too deep, so bind
        # the live socket under a short /tmp root used as UNIFABLE_DATA for this test.
        short = Path(tempfile.mkdtemp(dir="/tmp", prefix="uf-"))
        monkeypatch.setenv("UNIFABLE_DATA", str(short))
        jd = short / "judged"
        jd.mkdir()
        dead = jd / "dead.sock"
        dead.write_text("")  # plain file, not a listening socket -> not connectable
        _stale(dead)
        live = jd / "live.sock"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(str(live))
            srv.listen(1)
            _stale(live)
            janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=lambda pid: None)
            assert not dead.exists()
            assert live.exists(), "a connectable (live daemon) socket must be kept"
        finally:
            srv.close()
            with contextlib_suppress():
                live.unlink(missing_ok=True)
            shutil.rmtree(short, ignore_errors=True)

    def test_provenance_30d_not_24h(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
        ur = tmp_path / "unifusion-runs"
        ur.mkdir()
        young = ur / "young.md"
        young.write_text("x")
        _age(young, AGE_S + 3600)  # 25h old -> past state threshold but under 30d
        old = ur / "old.md"
        old.write_text("x")
        _age(old, PROV_AGE_S + 86400)  # 31d old
        janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=lambda pid: None)
        assert young.exists(), "provenance under 30d must be kept"
        assert not old.exists(), "provenance over 30d must be reaped"

    def test_never_touch_stable_runtime(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
        # bin/, versions/, current, and an arbitrary top-level file must survive
        # untouched (the janitor reaps specific subdirs/files only, never the
        # stable runtime root).
        (tmp_path / "bin").mkdir()
        binf = tmp_path / "bin" / "unifable"
        binf.write_text("#launcher")
        (tmp_path / "versions").mkdir()
        ver = tmp_path / "versions" / "1.0.0"
        ver.mkdir()
        stale_in_ver = ver / "junk.json"
        stale_in_ver.write_text("{}")
        _stale(stale_in_ver)
        cur = tmp_path / "current"
        cur.symlink_to(ver)
        top = tmp_path / "top-level.json"
        top.write_text("{}")
        janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=lambda pid: None)
        assert binf.exists() and binf.read_text() == "#launcher"
        assert stale_in_ver.exists(), "versions/ is stable runtime -- never reaped"
        assert cur.is_symlink()
        assert top.exists() and top.read_text() == "{}"

    def test_dead_alive_marker_reaped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))

        def raise_no_pid(pid, sig):
            raise ProcessLookupError

        monkeypatch.setattr(os, "kill", raise_no_pid)
        _write_marker(tmp_path, "dead-skey", host_pid=12345, project_root="/repo")
        marker = tmp_path / "alive" / "dead-skey.json"
        _stale(marker)
        janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=lambda pid: None)
        assert not marker.exists(), "a marker whose host is dead and is old must be reaped"

    def test_live_alive_marker_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        ps = _fake_ps({12345: (1, "claude", "claude")})
        _write_marker(tmp_path, "live-skey", host_pid=12345, project_root="/repo")
        marker = tmp_path / "alive" / "live-skey.json"
        _stale(marker)
        janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=ps)
        assert marker.exists(), "a marker for a currently-live session must be kept"


# ---------------------------------------------------------------------------
# DB row reap (skey + dir_hash protection)
# ---------------------------------------------------------------------------


def _insert_session(conn, skey: str, updated_at: str) -> None:
    conn.execute(
        "INSERT INTO sessions(skey, session_id, project_root, data, updated_at) VALUES(?,?,?,?,?)",
        (skey, "sid", "/repo", "{}", updated_at),
    )


def _insert_spec(conn, spec_key: str, updated_at: str) -> None:
    conn.execute(
        "INSERT INTO specs(spec_key, doc, updated_at) VALUES(?,?,?)",
        (spec_key, "{}", updated_at),
    )


class TestDbReap:
    def test_stale_unprotected_rows_deleted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
        from datetime import UTC, datetime, timedelta

        stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        fresh = datetime.now(UTC).isoformat()
        with db.connect() as conn:
            assert conn is not None
            with db._immediate(conn):
                _insert_session(conn, "stale-skey", stale)
                _insert_session(conn, "fresh-skey", fresh)
        janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=lambda pid: None)
        with db.connect() as conn:
            assert conn is not None
            sks = {r["skey"] for r in conn.execute("SELECT skey FROM sessions")}
        assert "stale-skey" not in sks
        assert "fresh-skey" in sks

    def test_live_skey_row_protected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        ps = _fake_ps({12345: (1, "claude", "claude")})
        from datetime import UTC, datetime, timedelta

        stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        repo = str(tmp_path / "repo")
        monkeypatch.setenv("UNIFABLE_PROJECT_ROOT", repo)
        skey = ledger.ledger_key({"session_id": "sid", "cwd": repo})
        _write_marker(tmp_path, skey, host_pid=12345, project_root=repo)
        with db.connect() as conn:
            assert conn is not None
            with db._immediate(conn):
                _insert_session(conn, skey, stale)
                _insert_session(conn, "other-skey", stale)
        janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=ps)
        with db.connect() as conn:
            assert conn is not None
            sks = {r["skey"] for r in conn.execute("SELECT skey FROM sessions")}
        assert skey in sks, "live session row must be protected"
        assert "other-skey" not in sks

    def test_spec_dir_hash_protected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UNIFABLE_DATA", str(tmp_path))
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        ps = _fake_ps({12345: (1, "claude", "claude")})
        from datetime import UTC, datetime, timedelta

        stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        repo = str(tmp_path / "repo")
        monkeypatch.setenv("UNIFABLE_PROJECT_ROOT", repo)
        dh = dir_hash(repo)
        skey = ledger.ledger_key({"session_id": "sid", "cwd": repo})
        _write_marker(tmp_path, skey, host_pid=12345, project_root=repo)
        with db.connect() as conn:
            assert conn is not None
            with db._immediate(conn):
                _insert_spec(conn, f"{dh}/sid", stale)  # protected (live dir_hash)
                _insert_spec(conn, "deaddeaddeaddead/other", stale)  # not protected
        janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=ps)
        with db.connect() as conn:
            assert conn is not None
            keys = {r["spec_key"] for r in conn.execute("SELECT spec_key FROM specs")}
        assert f"{dh}/sid" in keys, "spec under a live dir_hash must be protected"
        assert "deaddeaddeaddead/other" not in keys


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_run_never_raises_on_bad_root(self, tmp_path, monkeypatch):
        # data root points at a plain file -> subdir ops all OSError; must not raise.
        bad = tmp_path / "iamfile"
        bad.write_text("x")
        monkeypatch.setenv("UNIFABLE_DATA", str(bad))
        counts = janitor.run(age_s=AGE_S, provenance_age_s=PROV_AGE_S, max_reap=100, ps_provider=lambda pid: None)
        assert isinstance(counts, dict)

    def test_main_disabled_returns_zero(self, monkeypatch):
        monkeypatch.setenv("UNIFABLE_JANITOR", "0")
        assert janitor.main(["--run"]) == 0

    def test_main_no_run_flag_returns_zero(self):
        assert janitor.main([]) == 0


# small helper to avoid importing contextlib at top just for one call
import contextlib as _contextlib  # noqa: E402


def contextlib_suppress():
    return _contextlib.suppress(OSError)
